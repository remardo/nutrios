import re
from typing import Dict, Any, List

def parse_formatted_block(block: str) -> Dict[str, Any]:
    """
    Парсит наш фиксированный формат текстового отчёта в структуру.
    Возвращает: title, portion_g, confidence, kcal, macros, flags, micronutrients (top-5), assumptions.
    """
    data: Dict[str, Any] = {
        "title": "Блюдо",
        "portion_g": 300,
        "confidence": 60,
        "kcal": 360,
        "protein_g": None, "fat_g": None, "carbs_g": None,
        "flags": {"vegetarian": None, "vegan": None, "glutenfree": None, "lactosefree": None},
        "micronutrients": [], "assumptions": []
    }
    # title — вторая строка
    m = re.search(r"\n([^\n]+)\.\nПорция", block)
    if m: data["title"] = m.group(1).strip()

    # Порция и доверие
    m = re.search(r"Порция:\s*~\s*(\d+)\s*г\s*·\s*доверие\s*(\d+)%", block)
    if m:
        data["portion_g"] = int(m.group(1)); data["confidence"] = int(m.group(2))

    # Калории
    m = re.search(r"Калории:\s*(\d+)\s*ккал", block)
    if m: data["kcal"] = int(m.group(1))

    # БЖУ
    m = re.search(r"БЖУ:\s*белки\s*([\d]+)\s*г\s*·\s*жиры\s*([\d]+)\s*г\s*·\s*углеводы\s*([\d]+)\s*г", block)
    if m:
        data["protein_g"] = int(m.group(1)); data["fat_g"] = int(m.group(2)); data["carbs_g"] = int(m.group(3))

    # Флаги диеты
    m = re.search(r"vegetarian:\s*(да|нет).*vegan:\s*(да|нет)", block)
    if m:
        data["flags"]["vegetarian"] = (m.group(1) == "да"); data["flags"]["vegan"] = (m.group(2) == "да")
    m = re.search(r"glutenfree:\s*(да|нет).*lactosefree:\s*(да|нет)", block)
    if m:
        data["flags"]["glutenfree"] = (m.group(1) == "да"); data["flags"]["lactosefree"] = (m.group(2) == "да")

    # Микроэлементы (две строки минимум)
    micro_lines = []
    for line in block.splitlines():
        if line.strip().startswith("• "):
            micro_lines.append(line.strip()[2:])
    # Берём первые 5 пунктов раздела "Ключевые микроэлементы"
    after_header = False
    micro: List[str] = []
    for line in block.splitlines():
        if "Ключевые микроэлементы" in line:
            after_header = True; continue
        if after_header:
            if line.startswith("Флаги диеты:"): break
            if line.strip().startswith("• "): micro.append(line.strip()[2:])
    data["micronutrients"] = micro[:5]

    # Допущения (берём строки после "Допущения:")
    assum: List[str] = []
    take = False
    for line in block.splitlines():
        if line.startswith("Допущения:"):
            take = True; continue
        if take and line.strip().startswith("• "):
            assum.append(line.strip()[2:])
    data["assumptions"] = assum

    return data
