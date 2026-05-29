"""
ИСПРАВЛЕННЫЙ Winline парсер - с обработкой ошибок браузера

Исправления:
1. wait_until="load" вместо "domcontentloaded"
2. sleep(4) вместо sleep(2)
3. Проверка что страница живая перед query_selector
4. Retry логика для старта браузера
5. Более мягкие флаги браузера
6. Правильная обработка ошибок при закрытии браузера
"""

import asyncio
import gc
import json
import re
import psutil
import os
from collections import OrderedDict
from typing import Callable, Optional
from datetime import datetime

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from models import Event, EventStatus, Market, MarketType, Outcome, OutcomeType, Period, Platform

EVENTS_LIST_URL = "https://winline.ru/stavki/sport/kibersport"

PERIOD_MAP = {
    "матч": Period.FULL_MATCH,
    "1 карта": Period.MAP_1,
    "2 карта": Period.MAP_2,
    "3 карта": Period.MAP_3,
    "4 карта": Period.MAP_4,
    "5 карта": Period.MAP_5,
}

READ_ALL_EVENTS_JS = """
() => {
    const PRELIVE_MAX_SECS = 1200; // 20 минут (с запасом, чтобы не пропустить матчи из CGP)

    const PERIOD_MAP = {
        'матч':    'full_match',
        '1 карта': 'map_1',
        '2 карта': 'map_2',
        '3 карта': 'map_3',
        '4 карта': 'map_4',
        '5 карта': 'map_5',
    };

    // Парсим время начала Winline → секунды до старта.
    // Форматы: "Сегодня 10:00", "Сегодня, 10:00", "сегодня в 10:00"
    // Возвращает Infinity если время не сегодня (Завтра / дата).
    function parseWinlineTime(text) {
        // Гибкий regex для разных форматов записи "Сегодня"
        const m = text.match(/сегодня[\\s,]*(?:в\\s*)?(\\d{1,2}):(\\d{2})/i);
        if (!m) return Infinity;
        const now = new Date();
        const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
                               parseInt(m[1]), parseInt(m[2]), 0);
        const diff = Math.floor((start - now) / 1000);
        return diff < 0 ? 0 : diff; // 0 = уже началось сегодня
    }

    const result = [];

    for (const compDiv of document.querySelectorAll('.card__competitors')) {
        const link = compDiv.querySelector('a[href*="/stavki/event/"]');
        if (!link) continue;
        const eventId = link.href.split('/').pop();

        const nameWrapper = compDiv.querySelector('.body-left__names, [class*="names"]');
        let teams = [];
        if (nameWrapper) {
            const nameEls = nameWrapper.querySelectorAll('.name, [class*="name"]');
            if (nameEls.length >= 2) {
                teams = [...nameEls].map(function(el){ return el.innerText.trim(); }).filter(Boolean);
            }
            if (teams.length < 2) {
                teams = nameWrapper.innerText.trim().split(/\\n+/).map(function(t){ return t.trim(); }).filter(Boolean);
            }
        }
        if (teams.length < 2) continue;

        // Определяем live vs pre-live по классу карточки
        const cardEl = compDiv.closest('.card');
        if (!cardEl) continue;
        const isLive = cardEl.classList.contains('card--live');

        // Для pre-live: проверяем время начала из .header-left__time
        if (!isLive) {
            const timeEl = cardEl.querySelector('.header-left__time');
            if (!timeEl) continue; // Нет времени — пропускаем

            const secs = parseWinlineTime(timeEl.innerText.trim());
            // Пропускаем если матч не сегодня или начнётся позже чем через 20 мин
            if (secs > PRELIVE_MAX_SECS) continue;
        }

        const parent = compDiv.parentElement;
        if (!parent) continue;
        const bodies = parent.querySelectorAll('.card__body');

        const markets = [];
        for (const body of bodies) {
            const periodLabelEl =
                body.querySelector('.match-row-label') ||
                body.querySelector('.period-name');
            const periodText = periodLabelEl
                ? periodLabelEl.innerText.trim().toLowerCase()
                : '';
            const period = PERIOD_MAP[periodText] || null;
            if (!period) continue;

            const firstMarket = body.querySelector('.card__market');
            if (!firstMarket) continue;

            const btns = firstMarket.querySelectorAll('.coefficient-button_generic2');
            if (btns.length < 2) continue;

            const k1 = parseFloat(btns[0].innerText.trim());
            const k2 = parseFloat(btns[1].innerText.trim());
            if (isNaN(k1) || isNaN(k2) || k1 <= 1 || k2 <= 1) continue;

            markets.push({ period, k1, k2 });
        }

        if (markets.length === 0) continue;

        const tournamentBlock = compDiv.closest(
            'ww-feature-block-tournament-dsk, ww-block-tournament-dsk'
        );
        let sport = '';
        let tournament = '';
        if (tournamentBlock) {
            const titleEl = tournamentBlock.querySelector(
                '.block-tournament-header__title, .block-tournament__title'
            );
            const titleText = titleEl ? titleEl.innerText.trim() : '';
            const parts = titleText.split(' | ');
            sport      = parts[0] || '';
            tournament = parts[1] || '';
        }

        result.push({ eventId, homeTeam: teams[0], awayTeam: teams[1],
                      sport, tournament, markets, isLive });
    }
    return result;
}
"""

MUTATION_OBSERVER_JS = """
() => {
    if (window.__wl_observer) return;
    let _timer = null;
    let _inProgress = false;
 
    window.__wl_observer = new MutationObserver(() => {
        clearTimeout(_timer);
        if (_inProgress) return;
        
        _timer = setTimeout(() => {
            _inProgress = true;
            window.__wlChanged();
            _inProgress = false;
        }, 100);
    });
 
    const root = document.querySelector('.events-list, main, body');
    window.__wl_observer.observe(root || document.body, {
        subtree:           true,
        childList:         true,
        characterData:     true,   // ← КЛЮЧЕВОЕ: Angular меняет текст кэфа напрямую
        attributes:        false,
    });
}
"""


def _build_event(raw: dict) -> Optional[Event]:
    is_live = raw.get("isLive", True)  # По умолчанию считаем лайв если нет данных
    markets = []
    for m in raw.get("markets", []):
        try:
            period = Period(m["period"])
        except ValueError:
            continue
        market_type = (
            MarketType.MATCH_WINNER if period == Period.FULL_MATCH
            else MarketType.MAP_WINNER
        )
        markets.append(Market(
            market_type=market_type,
            period=period,
            is_live=is_live,
            outcomes=[
                Outcome(outcome_type=OutcomeType.HOME, odds=m["k1"]),
                Outcome(outcome_type=OutcomeType.AWAY, odds=m["k2"]),
            ],
        ))

    if not markets:
        return None

    return Event(
        platform=Platform.WINLINE,
        event_id=f"wl_{raw['eventId']}",
        sport=raw.get("sport", ""),
        tournament=raw.get("tournament", ""),
        home_team=raw["homeTeam"],
        away_team=raw["awayTeam"],
        status=EventStatus.LIVE if is_live else EventStatus.UPCOMING,
        markets=markets,
    )


class WinlineParser:
    def __init__(
            self,
            on_update: Callable[[Event], None],
            on_remove: Optional[Callable[[str], None]] = None,
    ):
        self._on_update = on_update
        self._on_remove = on_remove
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._running = False

        self._max_events = 500
        self._last_state: OrderedDict[str, dict] = OrderedDict()

        self._stats = {
            'refresh_count': 0,
            'gc_collections': 0,
            'events_evicted': 0,
        }

        self._process = psutil.Process(os.getpid())

        # Защита от конкурентных вызовов _refresh:
        # MutationObserver может срабатывать раньше чем предыдущий refresh завершился.
        # Без флага задачи накапливаются → память растёт, CPU грузится.
        self._refresh_running: bool = False
        self._refresh_pending: bool = False
        self._is_flushing:     bool = False  # Инициализируем здесь (ранее только в методе)

        # Вторая страница для сканирования pre-live событий по вкладкам дисциплин
        self._prelive_page: Optional[Page] = None
        self._prelive_wl_ids: set = set()  # IDs найденных pre-live событий

    async def _prelive_scan_loop(self):
        """
        Отдельный цикл поиска pre-live событий на Winline.

        Проблема: основная страница стоит на фильтре "Сейчас" (только live).
        Pre-live матчи (непопулярные, начало через ≤20 мин) видны только
        в конкретных вкладках дисциплин (CS, LoL, Dota2, Valorant и т.д.).

        Решение: вторая страница каждые 30 сек перебирает вкладки дисциплин
        и собирает pre-live события, не мешая основному live-мониторингу.
        """
        await asyncio.sleep(15)  # Даём основной странице подняться

        try:
            self._prelive_page = await self._context.new_page()
            await self._prelive_page.goto(EVENTS_LIST_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            print("[WL] Pre-live scanner: страница создана")
        except Exception as e:
            print(f"[WL] Pre-live scanner: не удалось создать страницу: {e}")
            return

        while self._running:
            try:
                # Пересоздаём страницу если она упала
                if not self._prelive_page or self._prelive_page.is_closed():
                    try:
                        self._prelive_page = await self._context.new_page()
                        await self._prelive_page.goto(EVENTS_LIST_URL, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(2)
                        print("[WL] Pre-live scanner: страница пересоздана")
                    except Exception as e2:
                        print(f"[WL] Pre-live scanner: не удалось пересоздать страницу: {e2}")
                        await asyncio.sleep(30)
                        continue

                current_prelive_ids: set = set()

                # Получаем список вкладок дисциплин (кроме Топ и Сейчас)
                disciplines = await self._prelive_page.evaluate("""
                    () => Array.from(document.querySelectorAll('.cybersport-filter__name'))
                              .map(el => el.innerText.trim())
                              .filter(t => t && t !== 'Топ' && t !== 'Сейчас')
                """)

                for disc in disciplines[:20]:
                    try:
                        tab = self._prelive_page.locator(
                            '.cybersport-filter__name', has_text=disc
                        ).first
                        await tab.click(timeout=3000)

                        # Ждём рендер Angular (уменьшено с 2.5с до 1.5с)
                        await asyncio.sleep(1.5)

                        # Читаем все события из этой вкладки
                        raw_list = await self._prelive_page.evaluate(READ_ALL_EVENTS_JS)
                        prelive_in_tab = sum(1 for r in raw_list if not r.get('isLive', True))
                        if prelive_in_tab:
                            print(f"[WL] Pre-live scan '{disc}': {prelive_in_tab} событий")

                        for raw in raw_list:
                            if raw.get('isLive', True):
                                continue  # Live-события уже отслеживает основная страница

                            eid = f"wl_{raw['eventId']}"
                            current_prelive_ids.add(eid)

                            prev = self._last_state.get(eid)
                            if prev == raw:
                                continue  # Ничего не изменилось

                            self._last_state[eid] = raw
                            event = _build_event(raw)
                            if event:
                                self._on_update(event)

                    except Exception as e:
                        print(f"[WL] Pre-live scan ошибка вкладки '{disc}': {e}")
                        continue

                # Удаляем pre-live события которых больше нет ни в одной вкладке
                # (матч начался и ушёл в live, или был отменён)
                stale_ids = self._prelive_wl_ids - current_prelive_ids
                for eid in stale_ids:
                    if eid in self._last_state and not self._last_state[eid].get('isLive', False):
                        # Уже не pre-live и не live → удаляем
                        self._last_state.pop(eid, None)
                        if self._on_remove:
                            self._on_remove(eid)

                self._prelive_wl_ids = current_prelive_ids

                if current_prelive_ids:
                    print(f"[WL] Pre-live scan: найдено {len(current_prelive_ids)} событий")

                # Освобождаем DOM renderer после скана — переходим на blank
                # Страница с 20 вкладками держит большой DOM, blank его очищает
                try:
                    if self._prelive_page and not self._prelive_page.is_closed():
                        await self._prelive_page.goto("about:blank", wait_until="commit")
                except Exception:
                    pass

            except Exception as e:
                print(f"[WL] Pre-live scan loop ошибка: {e}")

            await asyncio.sleep(15)  # Сканируем каждые 15 секунд (было 30)

    async def _playwright_memory_flusher(self):
        """Полностью пересоздает контекст браузера Винлайн раз в час"""
        self._is_flushing = False

        while self._running:
            await asyncio.sleep(1800)  # Каждые 30 минут (было 60)
            print("[WL] 🧹 Глубокая очистка: Пересоздаем контекст браузера...")
            self._is_flushing = True

            try:
                # 1. Убиваем старый контекст Винлайна
                if self._context:
                    await self._context.close()

                # 2. Создаем абсолютно новый чистый контекст
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                # 3. Возвращаем жесткую блокировку мусора (картинок, аналитики)
                async def route_interceptor(route, request):
                    if request.resource_type in ("image", "font", "media", "stylesheet", "manifest", "texttrack"):
                        await route.abort()
                        return
                    if any(domain in request.url for domain in
                           ["yandex", "adriver", "google", "vk.com", "facebook", "sentry"]):
                        await route.abort()
                        return
                    await route.continue_()

                await self._context.route("**/*",
                                          lambda route, req: asyncio.ensure_future(route_interceptor(route, req)))

                # 4. Открываем страницу
                self._page = await self._context.new_page()
                await self._page.expose_function("__wlChanged", self._on_dom_change)

                # 5. Грузим сайт (без клика "Сейчас" — захватываем live + pre-live)
                print("[WL] Перезагрузка страницы...")
                await self._page.goto(EVENTS_LIST_URL, wait_until="load", timeout=45000)
                await asyncio.sleep(4)

                # 6. Кликаем Сейчас чтобы видеть все live дисциплины
                await self._click_seychas()

                # 7. ЗАНОВО внедряем MutationObserver
                await self._page.evaluate(MUTATION_OBSERVER_JS)

                # 8. Принудительно парсим данные
                await self._refresh()

                print("[WL] ✅ Контекст полностью пересоздан, память абсолютно чиста.")
            except Exception as e:
                print(f"[WL] Ошибка при глубокой очистке: {e}")
            finally:
                self._is_flushing = False

    async def start(self):
        """Запуск с retry логикой"""
        self._running = True
        retry_count = 0
        max_retries = 3

        while retry_count < max_retries and self._running:
            try:

                await self._start_browser()

                # ---> НАПИСАТЬ ЭТО ЗДЕСЬ <---
                # Запускаем фоновую задачу глубокой очистки памяти
                asyncio.create_task(self._playwright_memory_flusher())



                return

            except Exception as e:
                retry_count += 1

                await self.stop()

                if retry_count < max_retries:
                    wait_time = 5 * retry_count

                    await asyncio.sleep(wait_time)
                else:
                    print("[WL] Max retries reached, stopping")
                    self._running = False

    async def _start_browser(self):
        """Внутренняя логика запуска браузера"""
        playwright = await async_playwright().start()

        # ← ИСПРАВЛЕНО: Более мягкие флаги браузера
        self._browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-sync",
                "--disable-translate",
                "--mute-audio",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--js-flags=--max-old-space-size=192",  # V8 heap ≤ 192 МБ
                "--memory-pressure-off",
                "--disk-cache-size=0",
                "--media-cache-size=0",
                "--disable-application-cache",
                "--disable-offline-auto-reload",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
            ],
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        async def route_interceptor(route, request):
            url = request.url.lower()
            resource_type = request.resource_type

            if resource_type in ("image", "font", "media", "stylesheet"):
                await route.abort()
                return

            blocked_domains = [
                "yandex.ru", "mc.yandex.ru", "adriver.ru", "google-analytics.com",
                "googletagmanager.com", "vk.com", "facebook.net", "sentry.io"
            ]

            if any(domain in url for domain in blocked_domains):
                await route.abort()
                return

            await route.continue_()

        await self._context.route("**/*", lambda route, req: asyncio.ensure_future(route_interceptor(route, req)))

        self._page = await self._context.new_page()
        await self._page.expose_function("__wlChanged", self._on_dom_change)

        try:
            await asyncio.wait_for(
                self._page.goto(EVENTS_LIST_URL, wait_until="load"),
                timeout=45000  # ← БЫЛО 30000, СТАЛО 45000
            )
        except asyncio.TimeoutError:
            print("[WL] Page load timeout, continuing anyway...")

        # ← ИСПРАВЛЕНО: sleep(4) вместо sleep(2)
        await asyncio.sleep(4)

        # ← ИСПРАВЛЕНО: Проверка что страница живая
        if self._page.is_closed():
            raise Exception("Page closed during startup")

        # Кликаем "Сейчас" — это показывает ALL live события по ВСЕМ дисциплинам
        # (Dota2, LoL, Valorant, CS2 и т.д., не только Топ-30)
        # Без этого клика страница показывает только "Топ" (30 событий)
        await self._click_seychas()

        # Первичное чтение
        print("[WL] Initial read...")
        await self._refresh()

        # Запускаем MutationObserver
        await self._page.evaluate(MUTATION_OBSERVER_JS)

        # Background задачи
        asyncio.ensure_future(self._keepalive_loop())
        asyncio.ensure_future(self._memory_monitor_loop())
        asyncio.ensure_future(self._prelive_scan_loop())

        print("[WL] ✅ Browser started successfully!")

    async def stop(self):
        self._running = False
        if self._prelive_page:
            try:
                await self._prelive_page.close()
            except:
                pass
        if self._page:
            try:
                await self._page.close()
            except:
                pass
        if self._context:
            try:
                await self._context.close()
            except:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except:
                pass

    async def _on_dom_change(self):
        if not self._running:
            return
        if not self._page or self._page.is_closed():  # None check перед is_closed()
            return
        if self._refresh_running:
            self._refresh_pending = True
            return
        await self._refresh()

    async def _click_seychas(self):
        """
        Кликаем фильтр "Сейчас" на Winline kibersport.
        Это Angular-элемент с классом .cybersport-filter__name, не <button>.
        Без этого клика видно только Топ-30, а live LoL/Dota2/Valorant скрыты.
        """
        try:
            clicked = await self._page.evaluate("""
                () => {
                    const filters = document.querySelectorAll('.cybersport-filter__name');
                    for (const el of filters) {
                        if (el.innerText.trim() === 'Сейчас') {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if clicked:
                await asyncio.sleep(3)
                print("[WL] ✅ Клик 'Сейчас' — показываем все live дисциплины")
            else:
                print("[WL] ⚠️ Кнопка 'Сейчас' не найдена")
        except Exception as e:
            print(f"[WL] _click_seychas error: {e}")

    async def _force_render_all(self):
        """
        Angular использует content-visibility: auto — элементы вне экрана
        не рендерятся в DOM. Прокручиваем страницу чтобы все события
        (LoL, Valorant, Dota2 и т.д.) попали в DOM перед парсингом.
        """
        try:
            await self._page.evaluate("""
                () => new Promise(resolve => {
                    const step = 600;
                    let pos = 0;
                    const height = () => document.body.scrollHeight;
                    const tick = () => {
                        window.scrollTo(0, pos);
                        pos += step;
                        if (pos < height()) {
                            requestAnimationFrame(tick);
                        } else {
                            window.scrollTo(0, 0);
                            resolve();
                        }
                    };
                    tick();
                })
            """)
            await asyncio.sleep(0.4)  # Даём Angular время отрендерить новые элементы
        except Exception as e:
            print(f"[WL] force_render error: {e}")

    async def _refresh(self):
        """Перечитать события со страницы"""
        if getattr(self, '_is_flushing', False):
            return
        if self._page.is_closed():
            print("[WL] Page is closed, stopping refresh")
            return
        if self._refresh_running:
            return

        self._refresh_running = True
        try:
            raw_list: list[dict] = await self._page.evaluate(READ_ALL_EVENTS_JS)
        except Exception as e:
            print(f"[WL] Ошибка чтения DOM: {e}")
            self._refresh_running = False
            return

        current_ids = set()

        for raw in raw_list:
            eid = f"wl_{raw['eventId']}"
            current_ids.add(eid)

            if len(self._last_state) >= self._max_events and eid not in self._last_state:
                oldest_id = next(iter(self._last_state))
                del self._last_state[oldest_id]
                self._stats['events_evicted'] += 1

            prev = self._last_state.get(eid)
            if prev == raw:
                continue

            self._last_state[eid] = raw
            event = _build_event(raw)
            if event:
                self._on_update(event)

        for eid in list(self._last_state.keys()):
            if eid not in current_ids:
                self._last_state.pop(eid)
                if self._on_remove:
                    self._on_remove(eid)

        self._stats['refresh_count'] += 1
        self._refresh_running = False  # Снимаем флаг

        # Если пришла мутация пока мы читали DOM — перечитываем немедленно
        if self._refresh_pending:
            self._refresh_pending = False
            asyncio.ensure_future(self._refresh())

        if self._stats['refresh_count'] % 50 == 0:
            gc.collect(2)
            self._stats['gc_collections'] += 1

    async def _keepalive_loop(self):
        """Каждые 8 сек — быстрый фоллбэк если MutationObserver пропустил изменение"""
        while self._running:
            await asyncio.sleep(8)
            if not self._running:
                break
            try:
                # ← ИСПРАВЛЕНО: Проверка что браузер живой
                if self._page and not self._page.is_closed():
                    await self._refresh()
                    await self._page.evaluate(MUTATION_OBSERVER_JS)
            except Exception as e:
                print(f"[WL] Keepalive error: {e}")
                try:
                    if self._page and not self._page.is_closed():
                        await self._page.goto(EVENTS_LIST_URL, wait_until="load", timeout=45000)
                        await asyncio.sleep(4)
                        await self._click_seychas()
                        await self._page.evaluate(MUTATION_OBSERVER_JS)
                        await self._refresh()
                except Exception as e2:
                    print(f"[WL] Reload error: {e2}")

    async def _cdp_gc(self, page=None) -> None:
        """CDP V8 GC для Winline страницы"""
        target = page or self._page
        if not target or target.is_closed():
            return
        try:
            session = await target.context.new_cdp_session(target)
            await session.send("HeapProfiler.enable")
            await session.send("HeapProfiler.collectGarbage")
            await session.detach()
        except Exception:
            pass

    async def _memory_monitor_loop(self):
        """Мониторинг памяти + CDP GC каждые 5 мин"""
        tick = 0
        while self._running:
            try:
                await asyncio.sleep(60)
                tick += 1

                gc.collect(2)

                # CDP GC каждые 5 минут
                if tick % 5 == 0:
                    await self._cdp_gc(self._page)
                    if self._prelive_page and not self._prelive_page.is_closed():
                        await self._cdp_gc(self._prelive_page)

                mem_mb = self._process.memory_info().rss / 1024 / 1024
                if mem_mb > 2000:
                    print(f"[WL] ⚠️  Python RSS {mem_mb:.0f}MB > 2GB! Force GC...")
                    gc.collect(2)
                    self._stats['gc_collections'] += 1

            except Exception as e:
                print(f"[WL] Memory monitor error: {e}")

    @property
    def _events(self):
        """Для совместимости с сервером"""
        return self._last_state
