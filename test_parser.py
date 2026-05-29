import asyncio
import time
from csgopositive_parser import CSGOPositiveParser, _EventState


async def run_stress_test():
    # 1. Создаем парсер-пустышку (без браузера)
    parser = CSGOPositiveParser(on_update=lambda x: None)

    # 2. Забиваем кэш 100 разными матчами, чтобы имитировать загруженный лайв
    print("Подготовка к стресс-тесту... Создаем 100 матчей.")
    for i in range(100):
        event_id = str(10000 + i)
        parser._events[event_id] = _EventState(
            event_id=event_id,
            game="cs2",
            tournament="Test Tournament",
            home_name=f"Team A {i}",
            away_name=f"Team B {i}",
            home_raw_id="1",
            away_raw_id="2"
        )

    # 3. Генерируем 10 000 сообщений с рандомными кэфами
    print("Генерация 10 000 пакетов данных WebSocket...")
    messages = []
    for i in range(10000):
        # Меняем кэфы от 1.50 до 2.50, берем случайные ID из наших 100 матчей
        event_id = str(10000 + (i % 100))
        k1 = 1.50 + (i % 100) / 100
        k2 = 2.50 - (i % 100) / 100

        # Чередуем статусы (открыт/закрыт) для максимальной нагрузки на логику
        status = "0" if i % 5 != 0 else "1"

        msg = f'42["koef_change", {{"id": "{event_id}", "bet_type": "win_1", "status": "{status}", "koef_1": "{k1}", "koef_2": "{k2}"}}]'
        messages.append(msg)

    print("🔥 СТАРТ СТРЕСС-ТЕСТА 🔥")
    start_time = time.perf_counter()

    # 4. Бомбардируем парсер данными
    for msg in messages:
        await parser._process_ws_frame(msg)

    end_time = time.perf_counter()
    total_time = end_time - start_time
    frames_per_second = 10000 / total_time

    print("========================================")
    print(f"✅ Обработано сообщений: 10 000")
    print(f"⏱ Общее время: {total_time:.4f} секунд")
    print(f"🚀 Скорость: {frames_per_second:.0f} обновлений в секунду")
    print("========================================")

    if frames_per_second > 5000:
        print("Вердикт: ИДЕАЛЬНО. Сканер выдержит любую нагрузку киберспорта.")
    else:
        print("Вердикт: Нормально, но есть куда ускорять (проверь, включен ли orjson).")


if __name__ == "__main__":
    asyncio.run(run_stress_test())