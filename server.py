"""
Веб-сервер: запускает Winline и CSGOPositive парсеры и транслирует
обновления через WebSocket. Ускоренная версия (uvloop + orjson).
"""

import asyncio
import os
from datetime import datetime

import sys
import orjson as json
import psutil
import uvicorn

# winloop — только Windows, uvloop — только Linux/Mac
if sys.platform == "win32":
    import winloop as _loop_lib
else:
    import uvloop as _loop_lib


MEM_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_log.csv")

async def memory_monitor(interval_seconds=60):
    """
    Мониторинг памяти: пишет CSV каждую минуту.
    Формат: timestamp,rss_mb,peak_mb,events_cgp,events_wl,clients
    Смотри итог утром: python memory_report.py
    """
    process = psutil.Process(os.getpid())
    peak_mb = 0.0
    start_time = datetime.now()
    start_mb = 0.0
    first = True

    # Заголовок CSV при первом запуске
    import csv, io
    header_needed = not os.path.exists(MEM_LOG) or os.path.getsize(MEM_LOG) == 0
    with open(MEM_LOG, "a", encoding="utf-8", newline="") as f:
        if header_needed:
            f.write("timestamp,total_mb,peak_mb,py_mb,chrome_mb,cgp_events,wl_events,ws_clients\n")
        f.write(f"# СТАРТ: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    while True:
        await asyncio.sleep(interval_seconds)

        # Python-процесс
        py_mb = process.memory_info().rss / 1024 / 1024

        # Chrome sub-процессы (браузеры Playwright)
        # На Linux-контейнерах может бросать PermissionError — оборачиваем надёжно
        chrome_mb = 0.0
        try:
            children = process.children(recursive=True)
            for child in children:
                try:
                    chrome_mb += child.memory_info().rss / 1024 / 1024
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass  # В некоторых Linux-контейнерах недоступно

        total_mb = py_mb + chrome_mb

        if first:
            start_mb = total_mb
            first = False
        peak_mb = max(peak_mb, total_mb)

        cgp_count = len(cgp._events) if cgp else 0
        wl_count  = len(winline._last_state) if winline else 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        row = f"{ts},{total_mb:.1f},{peak_mb:.1f},{py_mb:.1f},{chrome_mb:.1f},{cgp_count},{wl_count},{len(clients)}\n"
        try:
            with open(MEM_LOG, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception as e:
            print(f"[MEM] Ошибка лога: {e}")

        # Краткий вывод в консоль
        elapsed = datetime.now() - start_time
        h, rem = divmod(int(elapsed.total_seconds()), 3600)
        m = rem // 60
        trend = total_mb - start_mb
        trend_str = f"+{trend:.0f}" if trend >= 0 else f"{trend:.0f}"
        print(f"[MEM] {h:02d}:{m:02d} | TOTAL: {total_mb:.0f} MB "
              f"(py:{py_mb:.0f} + chrome:{chrome_mb:.0f}) "
              f"| Peak: {peak_mb:.0f} | Trend: {trend_str} MB")


# Быстрый Event Loop: winloop (Windows) или uvloop (Linux)
asyncio.set_event_loop_policy(_loop_lib.EventLoopPolicy())

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from typing import Optional

from models import Event
from winline_parser import WinlineParser
from csgopositive_parser import CSGOPositiveParser

app = FastAPI()
clients: set[WebSocket] = set()
winline: Optional[WinlineParser] = None
cgp: Optional[CSGOPositiveParser] = None
state: dict[str, dict] = {}


async def broadcast(data: str):
    disconnected = set()
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.add(ws)
    clients.difference_update(disconnected)


def on_update(event: Event):
    d = event.to_dict()
    state[event.event_id] = d
    # orjson.dumps возвращает bytes, поэтому делаем decode
    raw_bytes = json.dumps({"type": "update", "data": d})
    asyncio.ensure_future(broadcast(raw_bytes.decode('utf-8')))


def on_remove(event_id: str):
    state.pop(event_id, None)
    raw_bytes = json.dumps({"type": "remove", "event_id": event_id})
    asyncio.ensure_future(broadcast(raw_bytes.decode('utf-8')))


async def memory_watchdog(threshold_mb: int = 1500):
    """
    Аварийный watchdog: если общая память (Python + Chrome) превышает threshold,
    принудительно очищаем Python GC и логируем предупреждение.
    При > 2× threshold — завершаем процесс (systemd/supervisor перезапустит).
    """
    import gc as _gc
    process = psutil.Process(os.getpid())
    while True:
        await asyncio.sleep(120)  # Проверяем каждые 2 мин

        total_mb = process.memory_info().rss / 1024 / 1024
        try:
            for child in process.children(recursive=True):
                try:
                    total_mb += child.memory_info().rss / 1024 / 1024
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass  # Linux-контейнер без доступа к дочерним процессам

        if total_mb > threshold_mb * 2:
            print(f"[WATCHDOG] ⛔ КРИТИЧНО: {total_mb:.0f}MB > {threshold_mb*2}MB — завершаем процесс")
            os._exit(1)  # supervisor/systemd перезапустит
        elif total_mb > threshold_mb:
            print(f"[WATCHDOG] ⚠️  {total_mb:.0f}MB > {threshold_mb}MB — запускаем GC")
            _gc.collect(2)


def _load_config() -> dict:
    try:
        with open("config.json", encoding="utf-8") as f:
            return json.loads(f.read())
    except Exception:
        return {}


@app.on_event("startup")
async def startup():
    global winline, cgp
    asyncio.create_task(memory_monitor(interval_seconds=60))
    asyncio.create_task(memory_watchdog(threshold_mb=1500))
    cfg = _load_config()
    cgp_user = cfg.get("cgp_username")
    cgp_pass = cfg.get("cgp_password")

    winline = WinlineParser(on_update=on_update, on_remove=on_remove)
    asyncio.ensure_future(winline.start())

    cgp = CSGOPositiveParser(
        on_update=on_update,
        on_remove=on_remove,
        username=cgp_user,
        password=cgp_pass,
    )
    asyncio.ensure_future(cgp.start())


@app.on_event("shutdown")
async def shutdown():
    if winline:
        await winline.stop()
    if cgp:
        await cgp.stop()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    for event_dict in state.values():
        try:
            msg = json.dumps({"type": "update", "data": event_dict}).decode('utf-8')
            await ws.send_text(msg)
        except Exception:
            break
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
