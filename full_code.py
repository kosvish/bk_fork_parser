import os

# === НАСТРОЙКИ ===
# Имя итогового файла
OUTPUT_FILE = "project_code.txt"

# Папки, которые скрипт будет полностью игнорировать
IGNORED_DIRS = {
    '.git', '.idea', '.vscode', '__pycache__',
    'venv', '.venv', 'env', 'node_modules'
}

# Расширения файлов, которые попадут в итоговый текстовик
# (Включил .py, .json, .md, .txt и .env.example)
ALLOWED_EXTENSIONS = ('.py', '.json', '.md', '.txt', '.example')


def get_project_tree(startpath):
    """Генерирует структуру проекта в виде дерева."""
    tree = []
    for root, dirs, files in os.walk(startpath):
        # Удаляем игнорируемые папки, чтобы os.walk не заходил в них
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

        level = root.replace(startpath, '').count(os.sep)
        indent = ' ' * 4 * level
        tree.append(f"{indent}📁 {os.path.basename(root) or '.'}/")

        subindent = ' ' * 4 * (level + 1)
        for f in files:
            if f.endswith(ALLOWED_EXTENSIONS) and f != OUTPUT_FILE:
                tree.append(f"{subindent}📄 {f}")

    return "\n".join(tree)


def collect_code(startpath, output_file):
    """Собирает структуру и код в один файл."""
    with open(output_file, 'w', encoding='utf-8') as outfile:
        # 1. Записываем структуру проекта
        outfile.write("=" * 60 + "\n")
        outfile.write("СТРУКТУРА ПРОЕКТА\n")
        outfile.write("=" * 60 + "\n\n")
        outfile.write(get_project_tree(startpath))
        outfile.write("\n\n\n")

        # 2. Записываем содержимое файлов
        outfile.write("=" * 60 + "\n")
        outfile.write("ИСХОДНЫЙ КОД\n")
        outfile.write("=" * 60 + "\n\n")

        for root, dirs, files in os.walk(startpath):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]

            for file in files:
                if file.endswith(ALLOWED_EXTENSIONS):
                    # Пропускаем сам файл вывода (и сам скрипт-коллектор на всякий случай)
                    if file == OUTPUT_FILE or file == "collector.py":
                        continue

                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, startpath)

                    outfile.write(f"{'-' * 60}\n")
                    outfile.write(f"ФАЙЛ: {rel_path}\n")
                    outfile.write(f"{'-' * 60}\n\n")

                    try:
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"[Ошибка чтения файла: {e}]\n")

                    outfile.write("\n\n")


if __name__ == "__main__":
    current_dir = os.getcwd()
    print(f"⏳ Собираю код из папки: {current_dir}...")
    collect_code(current_dir, OUTPUT_FILE)
    print(f"✅ Готово! Весь код собран в файл: {OUTPUT_FILE}")