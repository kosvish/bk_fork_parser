import asyncio
import websockets
import time
import orjson as json


async def test_network_speed():
    uri = "ws://localhost:8000/ws"

    print("Подключение к серверу...")
    try:
        async with websockets.connect(uri) as ws:
            print("✅ Подключено! Слушаем поток данных...")

            msg_count = 0
            start_time = time.time()
            total_bytes = 0

            while True:
                msg = await ws.recv()
                msg_count += 1
                total_bytes += len(msg.encode('utf-8'))

                # Каждые 50 обновлений выводим статистику
                if msg_count % 50 == 0:
                    elapsed = time.time() - start_time
                    fps = msg_count / elapsed
                    mb_received = total_bytes / (1024 * 1024)
                    print(
                        f"📦 Принято пакетов: {msg_count} | 🚀 Скорость канала: {fps:.2f} пакетов/сек | 💾 Объем: {mb_received:.2f} MB")

    except Exception as e:
        print(f"Отключено: {e}")


if __name__ == "__main__":
    asyncio.run(test_network_speed())