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

        if self._username and self._password:
            await self._login()

        # Прямой WS — основной канал получения кэфов (быстро, без браузерного оверхеда)
        asyncio.ensure_future(self._direct_websocket_loop())
        # Playwright WS — резервный канал (на случай если прямой WS не работает)
        # Синхронизация событий и очистка памяти
        asyncio.ensure_future(self._sync_events_loop())
        asyncio.ensure_future(self._cleanup_loop())

        print("[CGP] ✅ Запущен (прямой WS + Playwright backup)")

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
                                # Рынок закрыт WS. Проверяем: не завис ли замок?
                                # Если WS закрыл >30 сек назад и DOM показывает
                                # валидные кэфы — значит ставка реально открыта,
                                # просто WS не прислал reopen (кэфы не изменились).
                                closed_at = self._ws_closed_at.get((eid, Period.FULL_MATCH), 0)
                                if time.monotonic() - closed_at > 30:
                                    is_mkt_live = (new_status == "LIVE")
                                    state.market_odds[Period.FULL_MATCH] = (k1, k2, True, is_mkt_live)
                                    cache_k = (eid, Period.FULL_MATCH)
                                    self._odds_cache[cache_k] = (round(k1, 3), round(k2, 3), True)
                                    self._ws_closed_at.pop(cache_k, None)
                                    self._on_update(self._build_event(state))
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

                # ── Проверка на "протухшие" live-рынки ───────────────────────────
                # Если live-рынок не получал WS-обновлений дольше порога —
                # значит CSGOPositive закрыл его БЕЗ отправки status=1.
                #
                # Пороги разные по дисциплинам:
                #   CS2/Valorant: раунды ~2 мин, кэфы меняются часто → порог 2-3 мин
                #   Dota2/LoL:    игра непрерывная, кэфы могут быть стабильны 5-10 мин
                #   ML/Other:     5 минут как разумный дефолт
                STALE_BY_GAME = {
                    "cs2":      60,    # 1 мин — раунды короткие, кэфы меняются часто
                    "valorant": 90,    # 1.5 мин
                    "dota2":    180,   # 3 мин — игра длиннее, но обновления есть
                    "lol":      180,   # 3 мин
                    "ml":       120,   # 2 мин
                }
                STALE_DEFAULT = 120  # 2 мин для неизвестных игр

                now_m = time.monotonic()
                startup_age = now_m - self._start_time
                stale_count = 0
                for state in self._events.values():
                    stale_limit_live    = STALE_BY_GAME.get(state.game, STALE_DEFAULT)
                    stale_limit_prelive = 180  # Pre-live маркеты: 3 минуты
                    for period, (mk1, mk2, mis_open, mis_live) in list(state.market_odds.items()):
                        if not mis_open:
                            continue  # Уже закрыт — пропускаем
                        # is_live=True → обычный threshold по игре
                        # is_live=False → pre-live threshold (3 мин)
                        stale_limit = stale_limit_live if mis_live else stale_limit_prelive

                        last_ws = state.market_last_ws.get(period, 0)

                        if last_ws == 0:
                            # Рынок сидирован только из DOM, WS ещё не подтвердил.
                            #
                            # Адаптивный grace period:
                            #   WS активен и уже получил 5+ фреймов от других рынков
                            #   → этот рынок явно закрыт → ждём только 10 сек
                            #
                            #   WS молчит или только стартует
                            #   → ждём 30 сек (дольше, но безопаснее)
                            ws_delivering = self._direct_ws_active and self._direct_ws_frames >= 5
                            grace = 10 if ws_delivering else 30
                            if startup_age > grace:
                                state.market_odds[period] = (mk1, mk2, False, mis_live)
                                self._on_update(self._build_event(state))
                                stale_count += 1
                                print(f"[CGP] 🔒 Unconfirmed ({state.game}): "
                                      f"{state.home_name} vs {state.away_name} | {period.value} "
                                      f"| WS молчал {startup_age:.0f}с "
                                      f"(grace={grace}с, ws_frames={self._direct_ws_frames})")
                        elif now_m - last_ws > stale_limit:
                            # WS был, но уже давно молчит → рынок закрылся без status=1
                            state.market_odds[period] = (mk1, mk2, False, mis_live)
                            self._on_update(self._build_event(state))
                            stale_count += 1
                            print(f"[CGP] 🔒 Stale ({state.game}): {state.home_name} vs {state.away_name} "
                                  f"| {period.value} | нет обновлений {now_m - last_ws:.0f}с")

                if prelive_logged:
                    print(f"[CGP] PRE-LIVE ({len(prelife_events)}): {', '.join(prelive_logged)}")
                print(f"[CGP] LIVE: {len(live_events)} | PRE-LIVE: {len(prelife_events)} | total: {len(self._events)}"
                      + (f" | stale closed: {stale_count}" if stale_count else ""))

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
            const liveSelector = '.event.live_betting[data-id], .event.line_event:not(.live_betting)[data-id]';
            for (const ev of document.querySelectorAll(liveSelector)) {
                const eid = ev.getAttribute('data-id');
                if (!eid) continue;
                if (ev.classList.contains('finished_event')) continue;

                const teams = ev.querySelectorAll('a.m_open');
                if (teams.length < 2) {
                    // Событие существует (.live_betting), но кнопки ставок исчезли →
                    // ставка ЗАКРЫТА (DOM отражает это мгновенно)
                    closedBets.push(eid);
                    continue;
                }
                live.push(buildEventData(ev, teams));
                liveIds.add(eid);
            }

            // ── 2. PRE-LIVE: события без .live_betting, таймер ≤ 20 мин ────
            // Это матчи которые скоро начнутся и ставки ещё не открылись
            // на CSGOPositive, но могут быть открыты на Winline.
            for (const ev of document.querySelectorAll('.event:not(.live_betting)[data-id]')) {
                const eid = ev.getAttribute('data-id');
                if (!eid || liveIds.has(eid)) continue;
                if (ev.classList.contains('finished') || ev.classList.contains('completed')) continue;

                // Берём таймер с атрибутом data-start для точного расчёта
                const timerEl = ev.querySelector('span.timer.timer_active');
                if (!timerEl) continue;

                const dataStart = timerEl.getAttribute('data-start');
                if (!dataStart) continue;

                const secsToStart = Math.floor(
                    (new Date(dataStart).getTime() - Date.now()) / 1000
                );
                // Матч уже начался или начнётся более чем через 20 мин — пропускаем
                if (secsToStart <= 0 || secsToStart > PRELIVE_MAX_SECS) continue;

                const teams = ev.querySelectorAll('a.m_open');
                if (teams.length < 2) continue;

                prelife.push({ ...buildEventData(ev, teams), secsToStart });
            }

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

            mem_mb = process.memory_info().rss / 1024 / 1024
            print(f"[CGP] Memory: {mem_mb:.0f} MB | events: {len(self._events)} | cache: {len(self._odds_cache)}")

    @property
    def _events_dict(self):
        """Для совместимости с сервером"""
        return self._events

    async def _fetch_event_odds(self, eid: str):
        """Fetch odds from bets.php if logged in (optional enhancement)"""
        if not self._logged_in:
            return
        try:
            # Это опциональное улучшение для получения коэффициентов с bets.php
            pass
        except:
            pass