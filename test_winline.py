"""
Запустить: python3 test_winline.py
Выводит все найденные события с рынками в JSON формате.
Ctrl+C для остановки.
"""

import asyncio
import json

from winline_parser import WinlineParser
from models import Event


def on_update(event: Event):
    print(json.dumps(event.to_dict(), ensure_ascii=False, indent=2))
    print("---")


async def main():
    parser = WinlineParser(on_update=on_update)
    try:
        await parser.start()
    except KeyboardInterrupt:
        await parser.stop()


if __name__ == "__main__":
    asyncio.run(main())
