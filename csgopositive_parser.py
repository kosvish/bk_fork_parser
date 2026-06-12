"""
OPTIMIZED CSGOPositive Parser - БЕЗ БУФЕРИЗАЦИИ + PRELIFE ПОДДЕРЖКА
- Минимальная задержка коэффициентов (100-200ms)
- LIVE события (текущие матчи)
- PRELIFE события (за 10-15 минут до старта)
- Ловим вилки на обоих типах матчей
"""

import asyncio
import re
import gc
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional
import orjson as json
import websockets

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from models import Event, EventStatus, Market, MarketType, Outcome, OutcomeType, Period, Platform

BASE_URL = "https://csgopositive.xyz"
MAIN_URL = f"{BASE_URL}/"

APP_ID_TO_GAME: dict[str, str] = {
    "730": "cs2", "101": "lol", "106": "ml",
    "570": "dota2", "103": "valorant", "21595": "valorant"
}

# Разбор <a>-тегов из ответа bets.php (для pre-live карт)
_BETS_A_RE = re.compile(r"<a\b([^>]*)>")
_BETS_MAP_TEXT_RE = re.compile(r"^Победа на карте #(\d+)$")


def _bt_to_period(bet_type: str) -> Optional[Period]:
    """Быстрое преобразование bet_type в Period"""
    bt = bet_type
    if bt.startswith("live:"):
        bt = bt[5:]

    if not re.match(r"^win_\d+$", bt):
        return None

    try:
        n = int(bt.split("_")[1])
        if n == 1:
            return Period.FULL_MATCH
        elif 2 <= n <= 6:
            return Period(f"map_{n - 1}")
    except:
        pass
    return None


@dataclass
class _EventState:
    """Состояние события с поддержкой LIVE/PRELIFE"""
    event_id: str
    game: str
    tournament: str
    home_name: str
    away_name: str
    home_raw_id: str
    away_raw_id: str
    status: str = "LIVE"  # "LIVE" или "PRELIFE" ← НОВОЕ!

    # Period -> (k1, k2, is_open, is_live)
    market_odds: dict[Period, tuple[float, float, bool, bool]] = field(default_factory=dict)
    # Время последнего WebSocket-обновления для каждого рынка
    market_last_ws: dict = field(default_factory=dict)


class CSGOPositiveParser:
    def __init__(
            self,
            on_update: Callable[[Event], None],
            on_remove: Optional[Callable[[str], None]] = None,
            username: Optional[str] = None,
            password: Optional[str] = None,
    ):
        self._on_update = on_update
        self._on_remove = on_remove
        self._username = username
        self._password = password
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._ws_page: Optional[Page] = None

        # Events
        self._events: OrderedDict[str, _EventState] = OrderedDict()
        self._max_events = 300
        self._running = False
        self._logged_in = False
        self._ws_ready = False

        # WS buffer (only on startup)
        self._ws_buffer: list[str] = []
        self._max_ws_buffer = 2000

        # Delta cache — держим маленьким: удаляем половину при достижении лимита
        self._odds_cache: dict = {}
        self._max_cache_size = 1000

        # Оптимизация: скролл только на первом запуске и раз в 60 сек
        self._initial_scan_done = False
        self._sync_count = 0

        # Таймер закрытия рынков — (eid, period) → время когда WS закрыл.
        self._ws_closed_at: dict = {}

        # Время старта — для grace period неподтверждённых рынков
        self._start_time = time.monotonic()

        # True когда прямой WS подключён и получает данные
        # В этом случае Playwright-перехватчик пропускаем
        self._direct_ws_active = False
        # Сколько koef_change фреймов получил прямой WS (для адаптивного grace period)
        self._direct_ws_frames = 0

        self._stats = {
            'ws_frames': 0,
            'gc_collections': 0,
            'updates_sent': 0,
        }
        self._reloading = False

    async def start(self):
        """Запуск парсера"""
        self._running = True
        retry_count = 0

        while retry_count < 2 and self._running:
            try:
                await self._start_impl()
                return
            except Exception as e:
                retry_count += 1
                print(f"[CGP] Error: {e}, retry {retry_count}")
                await self.stop()
                await asyncio.sleep(3)

    async def _start_impl(self):
        """Внутренняя реализация старта"""
        playwright = await async_playwright().start()

        self._browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-sync",
                "--disable-translate",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--js-flags=--max-old-space-size=192",  # V8 heap ≤ 192 МБ (было 256)
                "--memory-pressure-off",
                # Отключаем все кэши — главный источник роста памяти
                "--disk-cache-size=0",
                "--media-cache-size=0",
                "--disable-application-cache",
                "--disable-offline-auto-reload",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
            ],
        )

        # Большой viewport — чтобы как можно больше событий попало в видимую область
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # Блокируем ненужные ресурсы
        async def route_handler(route):
            if route.request.resource_type in ("image", "font", "media", "stylesheet"):
                await route.abort()
                return
            if any(x in route.request.url.lower() for x in ["google", "yandex"]):
                await route.abort()
                return
            await route.continue_()

        await self._context.route("**/*", route_handler)
        try:
            from pathlib import Path as _Path
            _cpath = _Path("cookies_csgopositive.json")
            if _cpath.exists():
                _raw = json.loads(_cpath.read_text(encoding="utf-8"))
                _fix = {"strict": "Strict", "lax": "Lax", "none": "None",
                        "no_restriction": "None", "unspecified": "Lax"}
                for _c in _raw:
                    _c["sameSite"] = _fix.get(str(_c.get("sameSite", "")).lower(), "Lax")
                    if _c.get("expires") in (None, -1):
                        _c.pop("expires", None)
                await self._context.add_cookies(_raw)
                self._logged_in = True
                print(f"[CGP] ✅ Cookies загружены: {len(_raw)} — сессия активна")
            else:
                pass
        except Exception as _e:
            print(f"[CGP] ⚠️ Ошибка загрузки cookies: {_e}")
        self._ws_page = await self._context.new_page()
        self._ws_page.on("websocket", self._on_websocket)

        await self._ws_page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=30000)
        # Ждём рендер JS-контента и прокручиваем страницу чтобы
        # CSGOPositive загрузил события всех дисциплин (Val/LoL/Dota2 ниже CS2)
        await asyncio.sleep(2)
        await self._ws_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        await self._ws_page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        if not self._logged_in and self._username and self._password:
            await self._login()

        # Прямой WS — основной канал получения кэфов (быстро, без браузерного оверхеда)
        asyncio.ensure_future(self._direct_websocket_loop())
        # Playwright WS — резервный канал (на случай если прямой WS не работает)
        # Синхронизация событий и очистка памяти
        asyncio.ensure_future(self._sync_events_loop())
        asyncio.ensure_future(self._cleanup_loop())
        asyncio.ensure_future(self._betsphp_poll_loop())



    async def stop(self):
        """Остановка"""
        self._running = False
        if self._ws_page:
            try:
                await self._ws_page.close()
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

    # ============= ПРЯМОЙ WEBSOCKET (основной, быстрый) =============

    async def _direct_websocket_loop(self):
        """
        Прямое подключение к WS серверу CSGOPositive минуя браузер.

        Протокол Socket.IO v3 (Engine.IO v3):
          1. Сервер шлёт "0{...}"  → мы отвечаем "40" (connect namespace)
          2. Сервер шлёт "40{...}" → подключение к namespace подтверждено
          3. Сервер шлёт "2"       → ping, мы отвечаем "3" (pong)
          4. Сервер шлёт "42[...]" → событие koef_change с кэфами

        БЕЗ шага 1 ("40") сервер молчит → все данные шли через медленный Playwright!
        """
        uri = "wss://ws.csgopositive.xyz/odds/socket.io/?EIO=3&transport=websocket"
        frames_received = 0

        while self._running:
            try:
                async with websockets.connect(
                        uri,
                        ping_interval=None,
                        max_size=2 * 1024 * 1024,
                ) as ws:
                    frames_received = 0

                    while self._running:
                        msg = await ws.recv()

                        if isinstance(msg, (bytes, bytearray)):
                            msg = msg.decode("utf-8", errors="replace")

                        if msg.startswith("0"):
                            # Engine.IO handshake → ОБЯЗАТЕЛЬНО отправляем Socket.IO namespace connect
                            await ws.send("40")

                        elif msg.startswith("40"):
                            # Namespace подключён — теперь сервер будет слать koef_change
                            self._direct_ws_active = True
                            print("[CGP] ⚡ Socket.IO namespace подключён — получаем кэфы напрямую!")

                        elif msg.startswith("2"):
                            await ws.send("3")  # Pong

                        elif msg.startswith("42"):
                            frames_received += 1
                            self._direct_ws_frames += 1
                            if frames_received == 1:
                                print("[CGP] ✅ Первый koef_change получен через прямой WS")

                            if not self._ws_ready:
                                if len(self._ws_buffer) < self._max_ws_buffer:
                                    self._ws_buffer.append(msg)
                                continue
                            await self._process_ws_frame_fast(msg)

            except Exception as e:
                self._direct_ws_active = False
                frames_received = 0
                self._direct_ws_frames = 0  # Сбрасываем — grace period снова 30с до переподключения
                print(f"[CGP] Прямой WS разрыв: {e}. Реконнект через 2с...")
                await asyncio.sleep(2)

    # ============= PLAYWRIGHT WS (резервный) =============

    def _on_websocket(self, ws):
        """WebSocket listener"""
        if "odds" not in ws.url:
            return
        ws.on("framereceived", lambda p: asyncio.ensure_future(self._on_ws_frame(p)))

    async def _on_ws_frame(self, frame):
        """
        Playwright-перехватчик WS (резервный канал).
        Если прямой WS активен — пропускаем, он быстрее и не создаёт очередь задач.
        """
        if self._direct_ws_active:
            return  # Прямой WS работает → этот канал не нужен

        try:
            raw = frame.body if hasattr(frame, "body") else frame
            if isinstance(raw, (bytes, bytearray)):
                data = raw.decode("utf-8", errors="replace")
            else:
                data = str(raw)

            if not self._ws_ready:
                if len(self._ws_buffer) < self._max_ws_buffer:
                    self._ws_buffer.append(data)
                return

            await self._process_ws_frame_fast(data)
        except Exception:
            pass

    async def _process_ws_frame_fast(self, data: str):
        """БЫСТРАЯ обработка коэффициентов (без задержек!)"""
        # Быстрый отсев
        if not data.startswith('42["koef_change"'):
            return

        try:
            # Парсим JSON быстро
            obj = json.loads(data[2:])[1]
        except:
            return

        event_id = obj.get("id")
        bet_type = obj.get("bet_type", "")



        period = _bt_to_period(bet_type)
        if period is None or event_id not in self._events:
            return

        try:
            k1 = float(obj.get("koef_1", 0))
            k2 = float(obj.get("koef_2", 0))
        except:
            return

        # ← DELTA-ФИЛЬТР: не отправляем если не изменилось
        cache_key = (event_id, period)
        # status "0" = рынок открыт, "1" = закрыт
        is_open = str(obj.get("status", "0")) == "0"
        is_live = bet_type.startswith("live:")
        new_values = (round(k1, 3), round(k2, 3), is_open)

        if self._odds_cache.get(cache_key) == new_values:
            return  # Дубль - не отправляем!

        # Очищаем кэш если переполнен
        if len(self._odds_cache) >= self._max_cache_size:
            to_delete = list(self._odds_cache.keys())[:len(self._odds_cache) // 2]
            for k in to_delete:
                del self._odds_cache[k]

        self._odds_cache[cache_key] = new_values

        # Трекаем время закрытия/открытия рынков
        if not is_open:
            self._ws_closed_at[cache_key] = time.monotonic()
        else:
            self._ws_closed_at.pop(cache_key, None)

        # Обновляем состояние + трекаем время последнего WS-обновления
        state = self._events[event_id]
        state.market_odds[period] = (k1, k2, is_open, is_live)

        state.market_last_ws[period] = time.monotonic()  # Отметка времени получения

        # ← КРИТИЧНО: отправляем обновление
        self._on_update(self._build_event(state))
        self._stats['updates_sent'] += 1

        # GC каждые 1000 фреймов (не слишком агрессивно)
        self._stats['ws_frames'] += 1
        if self._stats['ws_frames'] % 1000 == 0:
            gc.collect(0)  # Только gen-0, быстро (<1мс)

    # ============= SYNC EVENTS (LIVE + PRELIFE) =============

    async def _sync_events_loop(self):
        """Синхронизация LIVE и PRELIFE событий"""
        while self._running:
            if self._reloading:
                await asyncio.sleep(1)
                continue
            try:
                if not self._ws_page or self._ws_page.is_closed():
                    break

                need_scroll = not self._initial_scan_done or (self._sync_count % 30 == 0)
                self._sync_count += 1
                live_events, prelife_events, closed_bets = await self._get_all_events(full_scan=need_scroll)
                if need_scroll:
                    self._initial_scan_done = True

                current_ids = set()

                def _upsert(ev_data: dict, new_status: str):
                    eid = ev_data["id"]
                    current_ids.add(eid)
                    if eid not in self._events:
                        if len(self._events) >= self._max_events:
                            oldest_id = next(iter(self._events))
                            del self._events[oldest_id]
                        state = _EventState(
                            event_id=eid,
                            game=APP_ID_TO_GAME.get(ev_data.get("appId", ""), "cs2"),
                            tournament=ev_data.get("tournament", ""),
                            home_name=ev_data["homeName"],
                            away_name=ev_data["awayName"],
                            home_raw_id=ev_data["homeRawId"],
                            away_raw_id=ev_data["awayRawId"],
                            status=new_status,
                        )
                        self._events[eid] = state

                        # Сеедим начальные кэфы из DOM (CSGOPositive не шлёт
                        # начальное состояние через WS — только дельты).
                        k1 = ev_data.get("k1", 0)
                        k2 = ev_data.get("k2", 0)
                        if k1 > 1.0 and k2 > 1.0:
                            is_mkt_live = (new_status == "LIVE")
                            state.market_odds[Period.FULL_MATCH] = (k1, k2, True, is_mkt_live)
                            self._odds_cache[(eid, Period.FULL_MATCH)] = (round(k1, 3), round(k2, 3), True)
                            # НЕ ставим market_last_ws здесь → period остаётся = 0 (неподтверждён)
                            # Grace period 30с: если WS молчит → рынок был закрыт до старта
                            self._on_update(self._build_event(state))
                    else:
                        state = self._events[eid]
                        # Статус изменился (pre-live → live) — обновляем и отправляем
                        if state.status != new_status:
                            state.status = new_status
                            if state.market_odds:
                                self._on_update(self._build_event(state))

                        # Обновляем кэфы из DOM — НО ТОЛЬКО ЕСЛИ РЫНОК ОТКРЫТ.
                        # Ключевой баг: DOM не знает о состоянии замка (только WS знает).
                        # Если WS закрыл рынок (is_open=False), DOM всё равно покажет
                        # последние кэфы, и мы ошибочно перезапишем замок → вилка вернётся.
                        k1 = ev_data.get("k1", 0)
                        k2 = ev_data.get("k2", 0)
                        if k1 > 1.0 and k2 > 1.0 and Period.FULL_MATCH in state.market_odds:
                            old_k1, old_k2, old_is_open, old_is_live = state.market_odds[Period.FULL_MATCH]
                            if not old_is_open:
                                # Рынок закрыт по WS — НЕ трогаем. Только WS решает,
                                # когда открыть обратно (придёт status=0). DOM не знает
                                # о замках, поэтому из него рынок не разлипаем.
                                pass
                            elif abs(old_k1 - k1) > 0.001 or abs(old_k2 - k2) > 0.001:
                                # Рынок открыт, кэф изменился в DOM → обновляем
                                is_mkt_live = (new_status == "LIVE")
                                state.market_odds[Period.FULL_MATCH] = (k1, k2, True, is_mkt_live)
                                self._odds_cache[(eid, Period.FULL_MATCH)] = (round(k1, 3), round(k2, 3), True)
                                self._on_update(self._build_event(state))

                # LIVE события
                for ev in live_events:
                    _upsert(ev, "LIVE")

                # PRELIFE события — только матчи через ≤20 мин
                prelive_logged = []
                for ev in prelife_events:
                    _upsert(ev, "PRELIFE")
                    secs = ev.get("secsToStart", "?")
                    prelive_logged.append(f"{ev['homeName']} vs {ev['awayName']} ({secs}s)")

                # ── DOM-метод быстрого закрытия ставок (каждые 2 сек) ──────
                # Если событие есть в _events, но DOM уже не видит a.m_open → закрываем ВСЕ рынки.
                # Это даёт реакцию ~2 сек вместо 60-480с от stale detection.
                for eid in closed_bets:
                    if eid not in self._events:
                        continue
                    state = self._events[eid]
                    changed = False
                    for period, (mk1, mk2, mis_open, mis_live) in list(state.market_odds.items()):
                        if mis_open:
                            state.market_odds[period] = (mk1, mk2, False, mis_live)
                            changed = True
                    if changed:
                        self._on_update(self._build_event(state))
                        print(f"[CGP] 🔒 DOM-close: {state.home_name} vs {state.away_name} (a.m_open исчезли)")

                # Удаляем события которых больше нет
                for eid in list(self._events.keys()):
                    if eid not in current_ids:
                        state = self._events.pop(eid)
                        # Чистим все связанные таймеры (ws_closed_at, odds_cache)
                        stale_keys = [k for k in self._ws_closed_at if k[0] == eid]
                        for k in stale_keys:
                            del self._ws_closed_at[k]
                        cache_keys = [k for k in self._odds_cache if k[0] == eid]
                        for k in cache_keys:
                            del self._odds_cache[k]
                        if self._on_remove:
                            self._on_remove(eid)

                # Первый запуск: открываем буфер и реплееим все накопленные фреймы.
                # Теперь события уже в self._events → кэфы применятся корректно.
                if not self._ws_ready:
                    self._ws_ready = True
                    buf = self._ws_buffer[:]
                    self._ws_buffer.clear()
                    print(f"[CGP] Replaying {len(buf)} buffered WS frames...")
                    for frame_data in buf:
                        await self._process_ws_frame_fast(frame_data)
                    print(f"[CGP] Buffer replay done. Events with odds: "
                          f"{sum(1 for s in self._events.values() if s.market_odds)}")

                stale_count = 0
                if not self._direct_ws_active:
                    now_m = time.monotonic()
                    for state in self._events.values():
                        for period, (mk1, mk2, mis_open, mis_live) in list(state.market_odds.items()):
                            if not mis_open:
                                continue
                            last_ws = state.market_last_ws.get(period, 0)
                            if last_ws and now_m - last_ws > 90:
                                state.market_odds[period] = (mk1, mk2, False, mis_live)
                                self._on_update(self._build_event(state))
                                stale_count += 1
                    if stale_count:
                        print(f"[CGP] ⚠️ WS оборван — заглушено рынков: {stale_count}")



            except Exception as e:
                print(f"[CGP] Sync error: {e}")

            await asyncio.sleep(2)

    async def _get_all_events(self, full_scan: bool = True) -> tuple[list[dict], list[dict]]:
        """
        Получить LIVE + PRE-LIVE события с CSGOPositive.

        LIVE   = все события с классом .live_betting (оригинальная рабочая логика).
                 Это матчи в процессе И матчи с открытыми pre-match ставками —
                 оба типа нужны для поиска вилок.

        PRE-LIVE = события БЕЗ .live_betting у которых span.timer[data-start]
                   показывает ≤ 20 минут до старта. Это матчи которые вот-вот
                   начнутся и ставки на них уже могут открываться.
        """
        JS_GET_ALL_EVENTS = """
        () => {
            const PRELIVE_MAX_SECS = 1200; // 20 минут

            const getName = (el) => {
                const nameEl = el.querySelector('.team_name');
                return nameEl ? nameEl.innerText.trim() : '';
            };

            const buildEventData = (ev, teams) => {
                const eventNameEl = ev.querySelector('.event_name');

                // Читаем начальные кэфы прямо из DOM.
                // CSGOPositive НЕ шлёт начальное состояние через WebSocket —
                // только дельты при изменении. Без чтения DOM Valorant/LoL
                // никогда не появятся (у них кэфы стабильные).
                const k1Raw = teams[0].querySelector('span.sum.odds_icon')?.innerText ?? '0';
                const k2Raw = teams[1].querySelector('span.sum.odds_icon')?.innerText ?? '0';
                const k1 = parseFloat(k1Raw) || 0;
                const k2 = parseFloat(k2Raw) || 0;

                return {
                    id: ev.getAttribute('data-id'),
                    appId: ev.getAttribute('data-app_id') || '',
                    tournament: eventNameEl ? eventNameEl.innerText.trim() : '',
                    homeRawId: teams[0].getAttribute('data-raw_id') || '',
                    awayRawId: teams[1].getAttribute('data-raw_id') || '',
                    homeName: getName(teams[0]),
                    awayName: getName(teams[1]),
                    k1: isNaN(k1) ? 0 : k1,
                    k2: isNaN(k2) ? 0 : k2,
                };
            };

            const live = [];
            const prelife = [];
            const liveIds    = new Set();
            // DOM-метод быстрого закрытия: если событие в live_betting,
            // но у него нет a.m_open ссылок — ставки закрыты прямо сейчас.
            // Проверяется каждые 2 сек → реакция почти мгновенная.
            const closedBets = [];

            // ── 1. LIVE события ──────────────────────────────────────────────
            // CGP убрал live_betting / line_event. Теперь сортируем по data-start:
            //   data-start в прошлом (или нет таймера) → LIVE (матч идёт)
            //   data-start ≤ 20 мин в будущем          → PRE-LIVE
            //   data-start > 20 мин                    → пропускаем
            const liveSelector = '.event[data-id]:not(.finished_event):not(.live_betting_upcoming)';
            for (const ev of document.querySelectorAll(liveSelector)) {
                const eid = ev.getAttribute('data-id');
                if (!eid) continue;
                if (ev.classList.contains('finished_event')) continue;

                const teams = ev.querySelectorAll('a.m_open');
                if (teams.length < 2) {
                    // Событие есть, но кнопки ставок исчезли → ставка закрыта
                    closedBets.push(eid);
                    continue;
                }

                // Определяем статус по таймеру data-start
                const timerEl = ev.querySelector('span.timer.timer_active');
                let secsToStart = null;
                if (timerEl) {
                    const ds = timerEl.getAttribute('data-start');
                    if (ds) {
                        secsToStart = Math.floor((new Date(ds).getTime() - Date.now()) / 1000);
                    }
                }

                if (secsToStart === null || secsToStart <= 0) {
                    // Нет таймера или время прошло → матч идёт (LIVE)
                    live.push(buildEventData(ev, teams));
                    liveIds.add(eid);
                } else if (secsToStart <= PRELIVE_MAX_SECS) {
                    // Скоро начнётся → PRE-LIVE
                    prelife.push({ ...buildEventData(ev, teams), secsToStart });
                }
                // else: матч далеко → пропускаем
            }

            // PRE-LIVE теперь определяется в том же цикле выше через data-start

            return { live, prelife, closedBets };
        }
        """
        try:
            # Скролл нужен только при первом запуске и периодически (раз в ~60 сек).
            # Без него headless-браузер не рендерит Valorant/LoL/Dota2 (ниже CS2).
            # После первого скролла события остаются в DOM — повторный скролл не нужен.
            if full_scan:
                await self._ws_page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(0.15)
                await self._ws_page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.1)

            result = await self._ws_page.evaluate(JS_GET_ALL_EVENTS)
            return result.get("live", []), result.get("prelife", []), result.get("closedBets", [])
        except Exception as e:
            print(f"[CGP] _get_all_events error: {e}")
            return [], [], []  # 3 значения — live, prelife, closedBets

    async def _login(self):
        """Логин если нужен"""
        try:
            await self._ws_page.evaluate(
                "() => { const a = document.querySelector('a[href=\"#auth\"]'); if (a) a.click(); }"
            )
            await asyncio.sleep(3)

            username_escaped = self._username.replace("'", "\\'")
            password_escaped = self._password.replace("'", "\\'")

            await self._ws_page.evaluate(f"""
                () => {{
                    const forms = Array.from(document.querySelectorAll('form'));
                    for (const form of forms) {{
                        if (form.querySelector('input[name="password2"]')) continue;
                        const loginEl = form.querySelector('input[name="login"]');
                        const passEl  = form.querySelector('input[name="password"]');
                        if (!loginEl || !passEl) continue;
                        loginEl.value = '{username_escaped}';
                        passEl.value  = '{password_escaped}';
                        const btn = form.querySelector('[type="submit"], button');
                        if (btn) btn.click();
                        return;
                    }}
                }}
            """)

            await asyncio.sleep(5)
            self._logged_in = True
            print("[CGP] Login successful!")
        except:
            print("[CGP] Login skipped")

    # ============= BUILD EVENT =============

    def _build_event(self, state: _EventState) -> Event:
        """Построить Event с информацией о LIVE/PRELIFE"""
        markets = []
        for period, (k1, k2, is_open, is_live) in state.market_odds.items():
            markets.append(Market(
                market_type=(
                    MarketType.MATCH_WINNER if period == Period.FULL_MATCH
                    else MarketType.MAP_WINNER
                ),
                period=period,
                is_live=is_live,
                is_open=is_open,
                outcomes=[
                    Outcome(outcome_type=OutcomeType.HOME, odds=k1 if is_open else 0),
                    Outcome(outcome_type=OutcomeType.AWAY, odds=k2 if is_open else 0),
                ],
            ))

        return Event(
            platform=Platform.CSGOPOSITIVE,
            event_id=state.event_id,
            sport=state.game,
            tournament=state.tournament,
            home_team=state.home_name,
            away_team=state.away_name,
            status=EventStatus.LIVE if state.status == "LIVE" else EventStatus.UPCOMING,
            markets=markets,
        )

    async def _cdp_gc(self) -> None:
        """Принудительная сборка мусора V8 через Chrome DevTools Protocol.
        Освобождает JavaScript heap напрямую — самый эффективный способ."""
        if not self._ws_page or self._ws_page.is_closed():
            return
        try:
            session = await self._ws_page.context.new_cdp_session(self._ws_page)
            await session.send("HeapProfiler.enable")
            await session.send("HeapProfiler.collectGarbage")
            await session.detach()
        except Exception:
            pass

    async def _cleanup_loop(self):
        """Периодическая очистка памяти: Python GC + CDP V8 GC"""
        import psutil, os
        process = psutil.Process(os.getpid())
        tick = 0
        while self._running:
            await asyncio.sleep(60)
            tick += 1

            # Python GC каждую минуту
            gc.collect(2)

            # Чистим odds_cache от удалённых событий
            active_ids = set(self._events.keys())
            stale_keys = [k for k in self._odds_cache if k[0] not in active_ids]
            for k in stale_keys:
                del self._odds_cache[k]

            # CDP V8 GC каждые 5 минут
            if tick % 5 == 0:
                await self._cdp_gc()
            if tick % 20 == 0:
                self._reloading = True
                try:
                    await self._ws_page.goto(MAIN_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    self._initial_scan_done = False  # next sync пере-скроллит
                    await self._cdp_gc()
                    print("[CGP] ♻️ Страница перезагружена — Chrome heap сброшен")
                except Exception as e:
                    print(f"[CGP] reload error: {e}")
                finally:
                    self._reloading = False

            mem_mb = process.memory_info().rss / 1024 / 1024
            print(f"[CGP] Memory: {mem_mb:.0f} MB | events: {len(self._events)} | cache: {len(self._odds_cache)}")

    @property
    def _events_dict(self):
        """Для совместимости с сервером"""
        return self._events

    async def _fetch_event_odds(self, eid: str):
        """Сеет коэффициенты из bets.php. Обрабатывает 3 случая закрытия:
        1) ставка с замком (disabled) — приходит в ответе, ставим is_open=False;
        2) карта пропала из ответа — удаляем её;
        3) 'Нет доступных ставок' (оба ответа пустые) — закрываем ВСЕ рынки замком.
        Рейт-лимит/сбой (None) — не трогаем ничего."""
        if not self._logged_in or not self._context:
            return
        state = self._events.get(eid)
        if not state:
            return
        try:
            home = await self._betsphp_request(eid, 1)  # {Period: (koef, is_open, is_live)} | {} | None
            away = await self._betsphp_request(eid, 2)
        except Exception as e:
            print(f"[CGP] bets.php fail {eid}: {e}")
            return

        # Рейт-лимит или сбой (None): ответ невалиден — НЕ трогаем рынки.
        if home is None or away is None:
            return

        # Случай 3: "Нет доступных ставок" — обе стороны пустые.
        # Матч идёт, но позитив снял все рынки → закрываем ВСЁ замком (не удаляя),
        # иначе в сканере висят старые коэффициенты как ложная вилка.
        if not home and not away:
            changed = False
            for period, (mk1, mk2, mopen, mlive) in list(state.market_odds.items()):
                if mopen:
                    state.market_odds[period] = (mk1, mk2, False, mlive)
                    self._odds_cache[(eid, period)] = (round(mk1, 3), round(mk2, 3), False)
                    changed = True
            if changed:
                self._on_update(self._build_event(state))
            return

        changed = False
        now = time.monotonic()
        WS_FRESH = 25  # сек: WS трогал рынок недавно → bets.php не вмешивается
        all_p = set(home) | set(away)

        for period in all_p:
            last_ws = state.market_last_ws.get(period, 0)
            if last_ws and (now - last_ws) < WS_FRESH:
                continue  # WS активен на рынке — он владеет локом и скоростью
            h = home.get(period)
            a = away.get(period)
            if not h or not a:
                continue  # для вилки нужны ОБЕ стороны
            k1, open1, live1 = h
            k2, open2, live2 = a
            is_open = open1 and open2
            is_live = live1 or live2
            new_val = (k1, k2, is_open, is_live)
            if state.market_odds.get(period) != new_val:
                state.market_odds[period] = new_val
                self._odds_cache[(eid, period)] = (round(k1, 3), round(k2, 3), is_open)
                changed = True

        # Случай 2: карта пропала из ответа — удаляем (серию не трогаем).

        has_series = Period.FULL_MATCH in all_p
        has_any_map = any(p != Period.FULL_MATCH for p in all_p)
        if has_any_map and not has_series and Period.FULL_MATCH in state.market_odds:
            del state.market_odds[Period.FULL_MATCH]
            self._odds_cache.pop((eid, Period.FULL_MATCH), None)
            state.market_last_ws.pop(Period.FULL_MATCH, None)
            changed = True

        # Случай 2: карта пропала из ответа — удаляем (серию уже обработали выше).
        for period in list(state.market_odds.keys()):
            if period == Period.FULL_MATCH:
                continue
            if period not in all_p:
                del state.market_odds[period]
                self._odds_cache.pop((eid, period), None)
                state.market_last_ws.pop(period, None)
                changed = True

        if changed:
            self._on_update(self._build_event(state))

    async def _betsphp_request(self, eid: str, team_id: int) -> dict:
        """Один POST → {Period: (koef, is_open, is_live)} для серии и ВСЕХ карт.
        Делим ответ на блоки <div class="bet">, внутри каждого берём <a>...</a>
        ЖАДНО до </a> — иначе вложенный '>' в data-bet_text='<b>LIVE</b>...'
        обрезает тег и всё ломается. Период по тексту, замок по 'disabled'."""
        resp = await self._context.request.post(
            "https://csgopositive.xyz/lib/bets.php",
            form={"action": "get_koef", "event_id": str(eid),
                  "team_id": str(team_id), "lang": "RU"},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://csgopositive.xyz/",
                "Origin": "https://csgopositive.xyz",
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=8000,
        )
        if not resp.ok:
            return {}
        html = await resp.text()
        if "bet_error" in html or "Слишком частые" in html:
            print(f"[CGP] ⛔ RATE-LIMIT по eid={eid} team={team_id} — позитив режет частоту")
            return None
        if "no_available" in html or "Нет доступных ставок" in html:
            return {}
        out = {}
        for bet in re.split(r'<div class="bet">', html):
            a = re.search(r'<a\b(.*?)</a>', bet, re.DOTALL)
            if not a:
                continue
            inner = a.group(1)
            classes = " ".join(re.findall(r'class="([^"]*)"', inner))
            if "m_next" not in classes:
                continue
            bt = re.search(r'data-bet_text="(.*?)"\s+(?:data-gem|class)', inner, re.DOTALL)
            text = re.sub(r"<[^>]+>", "", bt.group(1)).strip() if bt else ""
            text = re.sub(r"^\s*LIVE\s*", "", text).strip()

            data_type = ""
            dt = re.search(r'data-type="([^"]*)"', inner)
            if dt:
                data_type = dt.group(1)
            is_live_flag = data_type.startswith("live:")

            # Период строго по тексту (data-type плавает — ему не верим)
            if re.search(r"Победа в (?:серии|матче)", text):
                period = Period.FULL_MATCH
            else:
                mm = re.search(r"Победа на карте #(\d+)", text)
                if not mm:
                    continue
                try:
                    period = Period(f"map_{int(mm.group(1))}")
                except ValueError:
                    continue

            # Коэффициент: data-gem, иначе span ПЕРЕД <a> в этом же блоке
            gem = re.search(r'data-gem="([\d.]+)"', inner)
            if gem:
                koef = float(gem.group(1))
            else:
                sp = re.search(r'class="koef[^"]*"[^>]*>\s*([\d.]+)\s*<', bet)
                if not sp:
                    continue
                koef = float(sp.group(1))
            if koef <= 1.0:
                continue

            out[period] = (koef, "disabled" not in classes, is_live_flag)
        return out

    async def _betsphp_poll_loop(self):
        """Раз в 5с опрашивает LIVE-события через bets.php ради pre-live карт.
        Запросы размазаны по времени (не все разом), чтобы не долбить сайт."""
        await asyncio.sleep(8)  # даём логину и событиям подняться
        while self._running:
            if not self._logged_in:
                await asyncio.sleep(5)
                continue
            live_ids = [e for e, s in self._events.items() if s.status == "LIVE"]

            if not live_ids:
                await asyncio.sleep(5)
                continue
            delay = max(0.15, 5.0 / len(live_ids))  # размазываем по 5 сек
            for e in live_ids:
                if not self._running:
                    break
                try:
                    await self._fetch_event_odds(e)
                except Exception as ex:
                    print(f"[CGP] bets.php loop err {e}: {ex}")
                await asyncio.sleep(delay)
