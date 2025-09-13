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
        "micronutrients": [], "assumptions": [],
        "extras": {"fats": {}, "fiber": {}}
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

    # Расширенный разбор жиров (если присутствует в блоке)
    # Примеры ожидаемых строк:
    # Жиры подробно: всего 20 г; насыщенные 6 г; мононенасыщенные 8 г; полиненасыщенные 5 г; транс <0.5 г
    # Омега: омега-6 4 г; омега-3 1 г (соотношение 4:1)
    fats = {}
    m = re.search(r"Жиры подробно:\s*всего\s*([\d.,]+)\s*г;\s*насыщенные\s*([\d.,<]+)\s*г;\s*мононенасыщенные\s*([\d.,<]+)\s*г;\s*полиненасыщенные\s*([\d.,<]+)\s*г;\s*транс\s*([\d.,<]+)\s*г", block, re.IGNORECASE)
    if m:
        def to_float(x: str):
            x = x.replace(",", ".").replace("<", "0.")
            try: return float(x)
            except: return None
        fats.update({
            "total": to_float(m.group(1)),
            "saturated": to_float(m.group(2)),
            "mono": to_float(m.group(3)),
            "poly": to_float(m.group(4)),
            "trans": to_float(m.group(5)),
        })
    m = re.search(r"Омега:\s*омега-?6\s*([\d.,]+)\s*г;\s*омега-?3\s*([\d.,]+)\s*г\s*\(соотношение\s*([\d:.]+)\)", block, re.IGNORECASE)
    if m:
        def to_f(x: str):
            x = x.replace(",", ".")
            try: return float(x)
            except: return None
        fats.update({"omega6": to_f(m.group(1)), "omega3": to_f(m.group(2)), "omega_ratio": m.group(3)})
    if fats:
        data["extras"]["fats"] = fats

    # Клетчатка (общая / растворимая / нерастворимая)
    # Пример: Клетчатка: всего 8 г (растворимая 3 г, нерастворимая 5 г)
    fiber = {}
    m = re.search(r"Клетчатка:\s*всего\s*([\d.,]+)\s*г\s*\(растворимая\s*([\d.,]+)\s*г,\s*нерастворимая\s*([\d.,]+)\s*г\)", block, re.IGNORECASE)
    if m:
        def tf(x):
            x = x.replace(",", ".")
            try: return float(x)
            except: return None
        fiber.update({"total": tf(m.group(1)), "soluble": tf(m.group(2)), "insoluble": tf(m.group(3))})
    elif (m2 := re.search(r"Клетчатка:\s*всего\s*([\d.,]+)\s*г", block, re.IGNORECASE)):
        x = m2.group(1).replace(",", ".")
        try:
            fiber.update({"total": float(x)})
        except:
            pass
    if fiber:
        data["extras"]["fiber"] = fiber

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
