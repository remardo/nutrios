import os, json, requests, pandas as pd, streamlit as st
from dotenv import load_dotenv

# подгружаем .env из корня проекта (если есть)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

# базовый адрес API c суффиксом /api
API = os.getenv("ADMIN_API_BASE", "http://127.0.0.1:8088/api")
KEY = os.getenv("ADMIN_API_KEY", "supersecret")
HDR = {"x-api-key": KEY}


# --- helpers ---

def _public_base() -> str:
    """Вернуть публичную базу (без /api)."""
    try:
        return API[:-4] if API.endswith("/api") else API
    except Exception:
        return API


def _safe_image_path(img_path: str) -> str | None:
    if not img_path:
        return None
    # абсолютный URL
    if isinstance(img_path, str) and (img_path.startswith("http://") or img_path.startswith("https://")):
        return img_path
    # абсолютный локальный путь (например, /media/...)
    if isinstance(img_path, str) and img_path.startswith("/"):
        return f"{_public_base()}{img_path}"
    # локальные файлы в bot/downloads (fallback)
    img_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot", "downloads"))
    full_path = img_path if os.path.isabs(img_path) else os.path.join(img_dir, img_path)
    return full_path if os.path.exists(full_path) else None


def _highlight_selected(s: pd.Series, selected_id: int | None):
    color = "background-color: #2b6cb0; color: white;"  # blue highlight
    return [color if (selected_id is not None and s.get("id") == selected_id) else "" for _ in s]


def _pretty(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return f"<{type(obj).__name__}>"


def run_app() -> None:
    st.set_page_config(page_title="Nutrios Admin", page_icon="??", layout="wide")
    st.title("?? Nutrios - админка нутрициолога")

    # --- data loading with basic error handling ---
    try:
        clients = requests.get(f"{API}/clients", headers=HDR, timeout=10).json()
    except Exception as e:
        st.error(f"Не удалось подключиться к API: {e}")
        st.stop()

    # проверяем формат ответа
    if isinstance(clients, dict):
        st.error(f"API /clients вернул ошибку: {clients}.\nПроверьте ADMIN_API_BASE (сейчас: {API}).")
        st.stop()

    # --- sidebar: client + filters ---
    with st.sidebar:
        st.header("Клиенты")
        if not clients:
            st.info("Пока нет клиентов. Загрузите данные и попробуйте снова.")
            st.stop()
        options = {
            (f'@{c["telegram_username"]} ({c["telegram_user_id"]})' if c["telegram_username"] else str(c["telegram_user_id"])): c["id"]
            for c in clients
        }
        choice = st.selectbox("Клиент", list(options.keys()))
        client_id = options[choice]

        # Load meals for client
        meals = requests.get(f"{API}/clients/{client_id}/meals", headers=HDR).json()
        if not meals:
            st.info("Рационы пока не загружены.")
            st.stop()

        df = pd.DataFrame(meals)
        # Robust ISO8601 parsing (handles fractional seconds, tz) and coercion
        df["captured_at"] = pd.to_datetime(df["captured_at"], format="ISO8601", errors="coerce")

        # Date range
        dmin, dmax = df["captured_at"].min().date(), df["captured_at"].max().date()
        col_date_from, col_date_to = st.columns(2)
        with col_date_from:
            date_from = st.date_input("Дата от", value=dmin, min_value=dmin, max_value=dmax)
        with col_date_to:
            date_to = st.date_input("Дата до", value=dmax, min_value=dmin, max_value=dmax)

        # Text search
        q = st.text_input("Поиск в названиях", "")

        # Flags filter (any flag present true)
        col1, col2 = st.columns(2)
        with col1:
            f1 = st.checkbox("вегетарианское", value=False, key="vegetarian")
            f2 = st.checkbox("веганское", value=False, key="vegan")
        with col2:
            f3 = st.checkbox("без глютена", value=False, key="glutenfree")
            f4 = st.checkbox("без лактозы", value=False, key="lactosefree")

        st.markdown("---")
        st.caption("Подсказка: в левом блоке 'жиры' можно выбрать нужные метрики.")

        # жиры. дополнительные поля (сегменты фильтра по жирам)
        st.subheader("Жиры - настройки")
        fat_options_map = {
            "Общее": "fats_total",
            "Насыщенные": "fats_saturated",
            "Моно": "fats_mono",
            "Поли": "fats_poly",
            "Транс": "fats_trans",
            "Омега‑6": "omega6",
            "Омега‑3": "omega3",
        }
        selected_fats = st.multiselect(
            "Показывать по жирам",
            options=list(fat_options_map.keys()),
            default=["Общее","Насыщенные","Моно","Поли"],
        )
        # цели (ориентиры)
        st.subheader("План (эталоны)")
        OMEGA_RATIO_MIN, OMEGA_RATIO_MAX = 1.0, 3.0
        FIBER_MIN, FIBER_MAX = 25.0, 35.0  # г/сутки
        st.caption(f"ω‑ratio целевой диапазон: {OMEGA_RATIO_MIN}–{OMEGA_RATIO_MAX}")
        st.caption(f"Клетчатка в сутки: {FIBER_MIN}–{FIBER_MAX} г")

    # --- apply filters ---
    mask = (df["captured_at"].dt.date >= date_from) & (df["captured_at"].dt.date <= date_to)
    if q:
        mask &= df["title"].str.contains(q, case=False, na=False)

    active_flags = {
        "vegetarian": f1,
        "vegan": f2,
        "glutenfree": f3,
        "lactosefree": f4,
    }
    for k, v in active_flags.items():
        if v:
            mask &= df["flags"].apply(lambda d: (d or {}).get(k) is True)

    df_f = df.loc[mask].sort_values("captured_at", ascending=False).reset_index(drop=True)

    # reset gallery page on filter change
    filter_key = f"{date_from}_{date_to}_{q}_{f1}{f2}{f3}{f4}"
    if "gallery_filter_key" not in st.session_state:
        st.session_state.gallery_filter_key = filter_key
    if st.session_state.gallery_filter_key != filter_key:
        st.session_state.gallery_filter_key = filter_key
        st.session_state.gallery_page = 1

    # --- main layout: gallery + table/details ---
    left, right = st.columns([2, 3], gap="large")

    if "selected_meal_id" not in st.session_state:
        st.session_state.selected_meal_id = None
    if "gallery_page" not in st.session_state:
        st.session_state.gallery_page = 1
    if "gallery_page_size" not in st.session_state:
        st.session_state.gallery_page_size = 9  # by default 3x3

    with left:
        st.subheader("Галерея по периоду")
        imgs = []
        for _, r in df_f.iterrows():
            p = _safe_image_path(r.get("image_path"))
            # Добавляем карточки даже без фото
            imgs.append((r, p))

        if not imgs:
            st.info("Нет ни одной подходящей записи или изображения не найдены.")
        else:
            # page size & nav (single level columns; no nested columns)
            c_size, c_label, c_first, c_prev, c_next, c_last = st.columns([2, 6, 1, 1, 1, 1])
            with c_size:
                page_size = st.selectbox(
                    "На странице",
                    options=[6, 9, 12, 15, 18],
                    index=[6, 9, 12, 15, 18].index(st.session_state.gallery_page_size)
                    if st.session_state.gallery_page_size in [6, 9, 12, 15, 18]
                    else 1,
                    key="gallery_page_size",
                )
            total_pages = max(1, (len(imgs) + page_size - 1) // page_size)
            # clamp page to available range
            st.session_state.gallery_page = max(1, min(st.session_state.gallery_page, total_pages))
            with c_label:
                st.markdown(
                    f"Страница **{st.session_state.gallery_page}** из **{total_pages}**  ·  всего: {len(imgs)}"
                )
            with c_first:
                if st.button("⟪", help="В начало", key="gal_first"):
                    st.session_state.gallery_page = 1
            with c_prev:
                if st.button("‹", help="Назад", key="gal_prev"):
                    st.session_state.gallery_page = max(1, st.session_state.gallery_page - 1)
            with c_next:
                if st.button("›", help="Вперёд", key="gal_next"):
                    st.session_state.gallery_page = min(total_pages, st.session_state.gallery_page + 1)
            with c_last:
                if st.button("⟫", help="В конец", key="gal_last"):
                    st.session_state.gallery_page = total_pages

            # slice for current page
            page = st.session_state.gallery_page
            start = (page - 1) * page_size
            end = start + page_size
            page_imgs = imgs[start:end]

            # grid 3xN
            ncols = 3
            rows = [page_imgs[i:i + ncols] for i in range(0, len(page_imgs), ncols)]
            for row in rows:
                cols = st.columns(ncols)
                for (r, p), col in zip(row, cols):
                    with col:
                        if p:
                            st.image(p, caption=None, use_column_width=True)
                        else:
                            st.caption("(без фото)")
                        st.caption(f"{r['captured_at'].strftime('%Y-%m-%d %H:%M')} · {r['title']}")
                        if st.button("Выбрать", key=f"sel_{r['id']}"):
                            st.session_state.selected_meal_id = r["id"]

    with right:
        st.subheader("Карточка выбранного блюда")

        # Selected details card
        sel_id = st.session_state.selected_meal_id
        if sel_id is not None:
            sel = df_f[df_f["id"] == sel_id]
            if not sel.empty:
                r = sel.iloc[0]
                c1, c2 = st.columns([2, 3])
                with c1:
                    ip = _safe_image_path(r.get("image_path"))
                    if ip:
                        st.image(ip, use_column_width=True)
                    else:
                        st.info("Нет изображения")
                with c2:
                    # Get targets for percentages
                    targets = get_targets(client_id, db)
                    kcal_pct = None
                    p_pct = None
                    f_pct = None
                    c_pct = None
                    if targets:
                        if targets.get("kcal_target") and r['kcal']:
                            kcal_pct = round((r['kcal'] / targets["kcal_target"]) * 100, 1)
                        if targets.get("protein_target_g") and r['protein_g']:
                            p_pct = round((r['protein_g'] / targets["protein_target_g"]) * 100, 1)
                        if targets.get("fat_target_g") and r['fat_g']:
                            f_pct = round((r['fat_g'] / targets["fat_target_g"]) * 100, 1)
                        if targets.get("carbs_target_g") and r['carbs_g']:
                            c_pct = round((r['carbs_g'] / targets["carbs_target_g"]) * 100, 1)

                    kcal_str = f"{r['kcal']}" + (f" ({kcal_pct}%)" if kcal_pct else "")
                    p_str = f"{r['protein_g']} г" + (f" ({p_pct}%)" if p_pct else "")
                    f_str = f"{r['fat_g']} г" + (f" ({f_pct}%)" if f_pct else "")
                    c_str = f"{r['carbs_g']} г" + (f" ({c_pct}%)" if c_pct else "")

                    st.markdown(
                        f"**{r['title']}**\n\n"
                        f"{r['captured_at'].strftime('%Y-%m-%d %H:%M')} · {r['portion_g']} г\n\n"
                        f"ккал: {kcal_str} · Б: {p_str} · Ж: {f_str} · У: {c_str}\n\n"
                    )
                    extras = r.get("extras") or {}
                    if extras:
                        fats = (extras or {}).get("fats") or {}
                        fiber = (extras or {}).get("fiber") or {}
                        special_groups = (extras or {}).get("special_groups") or {}
                        lines = []
                        if fats:
                            parts = []
                            if fats.get("total") is not None: parts.append(f"общее {fats['total']} г")
                            if fats.get("saturated") is not None: parts.append(f"насыщенные {fats['saturated']} г")
                            if fats.get("mono") is not None: parts.append(f"моно {fats['mono']} г")
                            if fats.get("poly") is not None: parts.append(f"поли {fats['poly']} г")
                            if fats.get("trans") is not None: parts.append(f"транс {fats['trans']} г")
                            if parts:
                                lines.append("жиры: " + "; ".join(parts))
                            if fats.get("omega6") is not None or fats.get("omega3") is not None or fats.get("omega_ratio"):
                                om = []
                                if fats.get("omega6") is not None: om.append(f"ω6 {fats['omega6']} г")
                                if fats.get("omega3") is not None: om.append(f"ω3 {fats['omega3']} г")
                                if fats.get("omega_ratio"): om.append(f"соотношение {fats['omega_ratio']}")
                                lines.append("омега: " + "; ".join(om))
                        if fiber:
                            parts = []
                            if fiber.get("total") is not None: parts.append(f"общее {fiber['total']} г")
                            if fiber.get("soluble") is not None: parts.append(f"растворимая {fiber['soluble']} г")
                            if fiber.get("insoluble") is not None: parts.append(f"нерастворимая {fiber['insoluble']} г")
                            if parts:
                                lines.append("клетчатка: " + ", ".join(parts))
                        if special_groups:
                            parts = []
                            if special_groups.get("cruciferous"): parts.append(f"крестоцветные: {special_groups['cruciferous']}")
                            if special_groups.get("iron_type"): parts.append(f"железо: {special_groups['iron_type']}")
                            if special_groups.get("antioxidants_count") is not None: parts.append(f"антиоксиданты: {special_groups['antioxidants_count']} упоминаний")
                            if parts:
                                lines.append("специальные группы: " + "; ".join(parts))
                        if lines:
                            st.caption("\n".join(lines))
                    st.caption("флаги: " + _pretty(r.get("flags")))
                    if r.get("micronutrients"):
                        st.caption("микронутриенты: " + ", ".join(r["micronutrients"]))
                st.divider()

        # Table with highlight
        df_show = df_f[ [
            "id", "captured_at", "title", "portion_g", "kcal", "protein_g", "fat_g", "carbs_g", "flags", "micronutrients", "source_type", "image_path"
        ]].copy()

        st.download_button(
            "Скачать CSV",
            data=df_show.to_csv(index=False).encode("utf-8-sig"),
            file_name="meals.csv",
            mime="text/csv",
        )

        styled = df_show.style.apply(_highlight_selected, axis=1, selected_id=st.session_state.selected_meal_id)
        st.dataframe(styled, use_container_width=True, height=360)

    st.markdown("---")

    # агрегаты по дням (если доступны extras/daily, показываем метрики)
    try:
        ex_daily = requests.get(f"{API}/clients/{client_id}/extras/daily", headers=HDR).json()
        edf = pd.DataFrame(ex_daily)
    except Exception:
        edf = pd.DataFrame()
    if not edf.empty:
        edf["period_start"] = pd.to_datetime(edf["period_start"], format="ISO8601", errors="coerce").dt.tz_localize(None)
        edf_f = edf[(edf["period_start"].dt.date >= date_from) & (edf["period_start"].dt.date <= date_to)].copy()
        if not edf_f.empty:
            last_row = edf_f.sort_values("period_start").iloc[-1]
            omega_ratio = last_row.get("omega_ratio_num")
            if (omega_ratio is None or (isinstance(omega_ratio, float) and pd.isna(omega_ratio))) and {"omega6","omega3"}.issubset(edf_f.columns):
                try:
                    omega_ratio = float(last_row["omega6"]) / float(last_row["omega3"]) if last_row["omega3"] else None
                except Exception:
                    omega_ratio = None
            fiber_total = last_row.get("fiber_total")

            cA, cB = st.columns(2)
            with cA:
                st.metric("ωratio (последний период)", f"{omega_ratio:.2f}" if omega_ratio is not None else "-")
                if omega_ratio is not None:
                    if OMEGA_RATIO_MIN <= omega_ratio <= OMEGA_RATIO_MAX:
                        st.caption("в целевом диапазоне")
                    elif omega_ratio < OMEGA_RATIO_MIN:
                        st.caption("ниже рекомендуемого")
                    else:
                        st.caption("выше рекомендуемого")
            with cB:
                st.metric("клетчатка, г/сутки (последний период)", f"{fiber_total:.0f}" if fiber_total is not None else "-")
                if fiber_total is not None:
                    if FIBER_MIN <= fiber_total <= FIBER_MAX:
                        st.caption("в целевом диапазоне")
                    elif fiber_total < FIBER_MIN:
                        st.caption("ниже нормы")
                    else:
                        st.caption("выше нормы")

    # --- Панель дня: выбранный день, суммарное потребление и нутриенты ---
    st.markdown("---")
    st.subheader("Панель дня")
    sel_day = st.date_input("День", value=dmax, min_value=dmin, max_value=dmax, key="day_panel")

    # Макросы за день
    try:
        daily_sum = requests.get(f"{API}/clients/{client_id}/summary/daily", headers=HDR).json()
        sdf = pd.DataFrame(daily_sum)
    except Exception:
        sdf = pd.DataFrame()
    row = None
    if not sdf.empty and "period_start" in sdf.columns:
        sdf["period_start"] = pd.to_datetime(sdf["period_start"]).dt.tz_localize(None)
        r = sdf[sdf["period_start"].dt.date == sel_day]
        if not r.empty:
            row = r.sort_values("period_start").iloc[-1]
    kcal = float(row["kcal"]) if row is not None else 0.0
    p_g = float(row["protein_g"]) if row is not None else 0.0
    f_g = float(row["fat_g"]) if row is not None else 0.0
    c_g = float(row["carbs_g"]) if row is not None else 0.0
    m1, m2, m3, m4 = st.columns(4)
    with m1: st.metric("Калории, ккал", f"{kcal:.0f}")
    with m2: st.metric("Белки, г", f"{p_g:.1f}")
    with m3: st.metric("Жиры, г", f"{f_g:.1f}")
    with m4: st.metric("Углеводы, г", f"{c_g:.1f}")

    # Проценты от калорийности: Б 10–35%, Ж 20–35% (целевой ≤30%), У 45–65%
    def pct(val_kcal: float, total_kcal: float) -> float | None:
        try:
            return round((val_kcal / total_kcal) * 100, 1) if total_kcal > 0 else None
        except Exception:
            return None

    p_pct = pct(p_g * 4, kcal)
    f_pct = pct(f_g * 9, kcal)
    c_pct = pct(c_g * 4, kcal)

    SAT_FAT_PCT_MAX = 10.0
    FAT_PCT_RANGE = (20.0, 35.0)
    FAT_PCT_TARGET_MAX = 30.0
    PROTEIN_PCT_RANGE = (10.0, 35.0)
    CARBS_PCT_RANGE = (45.0, 65.0)

    q1, q2, q3, q4 = st.columns(4)
    with q1: st.metric("Белки, % ккал", "-" if p_pct is None else f"{p_pct:.1f}%")
    with q2: st.metric("Жиры, % ккал", "-" if f_pct is None else f"{f_pct:.1f}%")
    with q3: st.metric("Углеводы, % ккал", "-" if c_pct is None else f"{c_pct:.1f}%")
    # насыщенные жиры %
    sat_pct = None
    # вычислим ниже из extras

    # Доп.поля: жиры и клетчатка за день
    try:
        daily_ex = requests.get(f"{API}/clients/{client_id}/extras/daily", headers=HDR).json()
        xdf = pd.DataFrame(daily_ex)
    except Exception:
        xdf = pd.DataFrame()
    fats_block, fiber_block = st.columns(2)
    day_fiber_total = None
    day_omega_ratio = None
    if not xdf.empty and "period_start" in xdf.columns:
        xdf["period_start"] = pd.to_datetime(xdf["period_start"]).dt.tz_localize(None)
        xr = xdf[xdf["period_start"].dt.date == sel_day]
        if not xr.empty:
            xr = xr.sort_values("period_start").iloc[-1]
            with fats_block:
                st.caption("Жиры за день")
                parts = []
                if pd.notna(xr.get("fats_total")): parts.append(f"общее {float(xr['fats_total']):.1f} г")
                if pd.notna(xr.get("fats_saturated")): parts.append(f"насыщенные {float(xr['fats_saturated']):.1f} г")
                # рассчёт доли насыщенных от калорий
                try:
                    _sat = float(xr.get("fats_saturated")) if pd.notna(xr.get("fats_saturated")) else None
                    sat_pct = pct((_sat or 0.0) * 9, kcal) if _sat is not None else None
                except Exception:
                    sat_pct = None
                if pd.notna(xr.get("fats_mono")): parts.append(f"моно {float(xr['fats_mono']):.1f} г")
                if pd.notna(xr.get("fats_poly")): parts.append(f"поли {float(xr['fats_poly']):.1f} г")
                if pd.notna(xr.get("fats_trans")): parts.append(f"транс {float(xr['fats_trans']):.1f} г")
                if pd.notna(xr.get("omega6")) or pd.notna(xr.get("omega3")) or pd.notna(xr.get("omega_ratio_num")):
                    parts.append(
                        "омега: "
                        + "; ".join(
                            [
                                f"ω6 {float(xr['omega6']):.1f} г" if pd.notna(xr.get("omega6")) else None,
                                f"ω3 {float(xr['omega3']):.1f} г" if pd.notna(xr.get("omega3")) else None,
                                f"ratio {float(xr['omega_ratio_num']):.2f}" if pd.notna(xr.get("omega_ratio_num")) else None,
                            ]
                        ).replace("None; ", "").replace("; None", "")
                    )
                if parts:
                    st.write("; ".join([p for p in parts if p]))
            with fiber_block:
                st.caption("Клетчатка за день")
                parts_f = []
                if pd.notna(xr.get("fiber_total")):
                    day_fiber_total = float(xr["fiber_total"])
                    parts_f.append(f"общее {day_fiber_total:.1f} г")
                if pd.notna(xr.get("fiber_soluble")): parts_f.append(f"растворимая {float(xr['fiber_soluble']):.1f} г")
                if pd.notna(xr.get("fiber_insoluble")): parts_f.append(f"нерастворимая {float(xr['fiber_insoluble']):.1f} г")
                if parts_f:
                    st.write("; ".join(parts_f))
            # capture omega ratio
            if pd.notna(xr.get("omega_ratio_num")):
                try:
                    day_omega_ratio = float(xr.get("omega_ratio_num"))
                except Exception:
                    day_omega_ratio = None

    # Качество питания: сравнение с коридорами
    st.caption("Легенда: 🟢 норма · 🟡 цель превышена · 🔴 вне коридора · ⚪ нет данных")
    st.caption("Коридоры: Б 10–35%, Ж 20–35% (целевой ≤30%), У 45–65%, насыщенные ≤10%, ω‑6:ω‑3 ≤3, клетчатка 25–35 г.")
    qual_lines: list[str] = []
    def _mark(ok: bool | None, warn: bool | None = None) -> str:
        if ok is True:
            return "🟢"
        if warn:
            return "🟡"
        return "🔴"
    # protein
    if p_pct is not None:
        ok = PROTEIN_PCT_RANGE[0] <= p_pct <= PROTEIN_PCT_RANGE[1]
        qual_lines.append(f"{_mark(ok)} Белки: {p_pct:.1f}% (норма {PROTEIN_PCT_RANGE[0]}–{PROTEIN_PCT_RANGE[1]}%)")
    # fat
    if f_pct is not None:
        ok = FAT_PCT_RANGE[0] <= f_pct <= FAT_PCT_RANGE[1]
        warn = ok and f_pct > FAT_PCT_TARGET_MAX
        qual_lines.append(f"{_mark(ok, warn)} Жиры: {f_pct:.1f}% (норма {FAT_PCT_RANGE[0]}–{FAT_PCT_RANGE[1]}%, цель ≤{FAT_PCT_TARGET_MAX}%)")
    # carbs
    if c_pct is not None:
        ok = CARBS_PCT_RANGE[0] <= c_pct <= CARBS_PCT_RANGE[1]
        qual_lines.append(f"{_mark(ok)} Углеводы: {c_pct:.1f}% (норма {CARBS_PCT_RANGE[0]}–{CARBS_PCT_RANGE[1]}%)")
    # saturated
    if sat_pct is not None:
        ok = sat_pct <= SAT_FAT_PCT_MAX
        qual_lines.append(f"{_mark(ok)} Насыщенные жиры: {sat_pct:.1f}% (≤{SAT_FAT_PCT_MAX}%)")

    # Спецгруппы: крестоцветные, железо (гем/негем), антиоксиданты
    st.markdown("---")
    st.subheader("Специальные группы за день")
    df_day = df[df["captured_at"].dt.date == sel_day]
    titles = [str(t).lower() for t in (df_day["title"].tolist() if not df_day.empty else [])]
    micro_lists = []
    if not df_day.empty and "micronutrients" in df_day.columns:
        for _, rr in df_day.iterrows():
            items = rr.get("micronutrients") or []
            if isinstance(items, list):
                micro_lists.extend([str(x).lower() for x in items if x])

    # 1) Крестоцветные (по ключевым словам в названии блюда/микро)
    crucifer_kw = {
        # RU
        "брокколи","цветная капуста","капуста","брюссельская капуста","кейл","листовая капуста",
        "пекинская капуста","пак-чой","пак чой","кольраби","редис","редька","руккола","кресс",
        # EN
        "broccoli","cauliflower","cabbage","brussels sprouts","kale","bok choy","pak choi",
        "collard","kohlrabi","radish","arugula","rocket","mustard greens","turnip greens","watercress",
    }
    def has_crucifer(text: str) -> bool:
        return any(k in text for k in crucifer_kw)
    crucifer_meals = 0
    if titles:
        for t in titles:
            if has_crucifer(t):
                crucifer_meals += 1
    # также проверим микронутриенты текстом
    if micro_lists:
        if any(has_crucifer(x) for x in micro_lists):
            # увеличим как минимум на 1, если в микро встречались
            crucifer_meals = max(crucifer_meals, 1)

    # 2) Железо: оценка гемового/негемового
    iron_kw = {"железо","iron","гемовое железо","негемовое железо","heme iron","non-heme iron"}
    meat_fish_kw = {
        # RU
        "говядина","телятина","свинина","баранина","печень","сердце","курица","индейка","утка",
        "рыба","семга","лосось","тунец","сардина","печень трески","печень индейки","стейк",
        # EN
        "beef","veal","pork","lamb","liver","offal","chicken","turkey","duck",
        "fish","salmon","tuna","sardine","cod liver","steak",
    }
    def is_meat_fish(text: str) -> bool:
        return any(k in text for k in meat_fish_kw)

    heme_cnt = 0
    nonheme_cnt = 0
    if not df_day.empty:
        for _, rr in df_day.iterrows():
            mic = [str(x).lower() for x in (rr.get("micronutrients") or []) if x]
            has_iron = any(("железо" in x) or ("iron" in x) for x in mic)
            if not has_iron:
                continue
            title_l = str(rr.get("title") or "").lower()
            flags = rr.get("flags") or {}
            is_veg = bool((flags or {}).get("vegan") or (flags or {}).get("vegetarian"))
            if is_meat_fish(title_l) and not is_veg:
                heme_cnt += 1
            else:
                nonheme_cnt += 1

    # 3) Антиоксиданты (без специфики) — считаем упоминания
    antioxidants_kw = {
        # RU
        "витамин c","аскорбиновая кислота","витамин е","токоферол","каротиноиды","бета-каротин",
        "ликопин","лютеин","зеаксантин","селен","полифенолы","флавоноиды","ресвератрол",
        "кверцетин","антоцианы","катехины",
        # EN
        "vitamin c","ascorbic acid","vitamin e","tocopherol","carotenoids","beta-carotene",
        "lycopene","lutein","zeaxanthin","selenium","polyphenols","flavonoids","resveratrol",
        "quercetin","anthocyanins","catechins",
    }
    antiox_mentions = 0
    if micro_lists:
        for x in micro_lists:
            if any(k in x for k in antioxidants_kw):
                antiox_mentions += 1

    mA, mB, mC = st.columns(3)
    with mA:
        st.metric("Крестоцветные, блюд", f"{crucifer_meals}")
    with mB:
        st.metric("Железо: гем/негем", f"{heme_cnt}/{nonheme_cnt}")
    with mC:
        st.metric("Антиоксиданты, упоминания", f"{antiox_mentions}")
    # итоги коридоров
    if qual_lines:
        st.write("\n".join(qual_lines))

    # Микронутриенты и аминокислоты за день по данным блюд
    micro_items: list[str] = []
    if not df_day.empty and "micronutrients" in df_day.columns:
        for _, rr in df_day.iterrows():
            items = rr.get("micronutrients") or []
            if isinstance(items, list):
                micro_items.extend([str(x) for x in items if x])
    if micro_items:
        # классификация аминокислот
        aa_ru = {
            "лейцин","изолейцин","валин","лизин","метионин","фенилаланин","треонин","триптофан","гистидин",
            "цистеин","тирозин","аргинин","глицин","аланин","серин","пролин","аспарагиновая кислота","глутаминовая кислота","глутамин","аспарагин"
        }
        aa_en = {
            "leucine","isoleucine","valine","lysine","methionine","phenylalanine","threonine","tryptophan","histidine",
            "cysteine","tyrosine","arginine","glycine","alanine","serine","proline","aspartic acid","glutamic acid","glutamine","asparagine"
        }
        aa_set = aa_ru | aa_en
        from collections import Counter
        norm = [x.strip().lower() for x in micro_items]
        aa_counts = Counter([x for x in norm if x in aa_set])
        other_counts = Counter([x for x in norm if x not in aa_set])

        c_aa, c_other = st.columns(2)
        if aa_counts:
            import pandas as _pd
            st.caption("Аминокислоты за день (топ)")
            with c_aa:
                aa_df = _pd.DataFrame({"name": list(aa_counts.keys()), "count": list(aa_counts.values())})
                aa_df = aa_df.sort_values("count", ascending=False).head(10)
                st.bar_chart(aa_df.set_index("name")["count"])
        if other_counts:
            import pandas as _pd
            st.caption("Прочие микронутриенты за день (топ)")
            with c_other:
                o_df = _pd.DataFrame({"name": list(other_counts.keys()), "count": list(other_counts.values())})
                o_df = o_df.sort_values("count", ascending=False).head(10)
                st.bar_chart(o_df.set_index("name")["count"])

    # Сводка "За день"
    st.markdown("---")
    st.subheader("За день — сводка")
    # Подсветка коридоров для БЖУ, насыщенных, клетчатки и ω‑ratio
    def _emo(ok: bool | None, warn: bool = False) -> str:
        if ok is True:
            return "🟢"
        if warn:
            return "🟡"
        if ok is False:
            return "🔴"
        return "⚪"

    prot_ok = (p_pct is not None and PROTEIN_PCT_RANGE[0] <= p_pct <= PROTEIN_PCT_RANGE[1])
    fat_ok = (f_pct is not None and FAT_PCT_RANGE[0] <= f_pct <= FAT_PCT_RANGE[1])
    fat_warn = bool(fat_ok and f_pct and f_pct > FAT_PCT_TARGET_MAX)
    carb_ok = (c_pct is not None and CARBS_PCT_RANGE[0] <= c_pct <= CARBS_PCT_RANGE[1])
    sat_ok = (sat_pct is not None and sat_pct <= SAT_FAT_PCT_MAX)
    fiber_ok = (day_fiber_total is not None and FIBER_MIN <= day_fiber_total <= FIBER_MAX)
    omega_ok = (day_omega_ratio is not None and day_omega_ratio <= OMEGA_RATIO_MAX)

    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    with r1c1:
        st.metric("Калории", f"{kcal:.0f} ккал")
    with r1c2:
        st.metric(f"{_emo(prot_ok)} Белки", f"{p_g:.1f} г", delta=(f"{p_pct:.1f}%" if p_pct is not None else None))
    with r1c3:
        st.metric(f"{_emo(fat_ok, fat_warn)} Жиры", f"{f_g:.1f} г", delta=(f"{f_pct:.1f}%" if f_pct is not None else None))
    with r1c4:
        st.metric(f"{_emo(carb_ok)} Углеводы", f"{c_g:.1f} г", delta=(f"{c_pct:.1f}%" if c_pct is not None else None))

    r2c1, r2c2, r2c3 = st.columns(3)
    with r2c1:
        st.metric(f"{_emo(sat_ok if sat_pct is not None else None)} Насыщенные жиры", (f"{sat_pct:.1f}%" if sat_pct is not None else "-"))
    with r2c2:
        st.metric(f"{_emo(fiber_ok if day_fiber_total is not None else None)} Клетчатка", (f"{day_fiber_total:.0f} г" if day_fiber_total is not None else "-"))
    with r2c3:
        st.metric(f"{_emo(omega_ok if day_omega_ratio is not None else None)} ω‑6:ω‑3", (f"{day_omega_ratio:.2f}" if day_omega_ratio is not None else "-"))

    r3c1, r3c2, r3c3 = st.columns(3)
    with r3c1:
        st.metric("Крестоцветные", f"{crucifer_meals}")
    with r3c2:
        st.metric("Железо (гем/негем)", f"{heme_cnt}/{nonheme_cnt}")
    with r3c3:
        st.metric("Антиоксиданты", f"{antiox_mentions}")

    # Сообщение для Telegram
    try:
        from bot.day_summary import format_day_summary_message
    except Exception:
        format_day_summary_message = None  # type: ignore
    if format_day_summary_message is not None:
        summary_payload = {
            "date": str(sel_day),
            "kcal": kcal,
            "protein_g": p_g,
            "fat_g": f_g,
            "carbs_g": c_g,
            "p_pct": p_pct,
            "f_pct": f_pct,
            "c_pct": c_pct,
            "sat_pct": sat_pct,
            "fiber_total": day_fiber_total,
            "omega_ratio": day_omega_ratio,
            "crucifer_meals": crucifer_meals,
            "heme_iron_meals": heme_cnt,
            "nonheme_iron_meals": nonheme_cnt,
            "antioxidants_mentions": antiox_mentions,
        }
        msg = format_day_summary_message(summary_payload)
        st.subheader("Сообщение для Telegram")
        st.text_area("Готовый текст", value=msg, height=220)

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Сводка БЖУ - по дням")
        daily = requests.get(f"{API}/clients/{client_id}/summary/daily", headers=HDR).json()
        ddf = pd.DataFrame(daily)
        if not ddf.empty:
            ddf["period_start"] = pd.to_datetime(ddf["period_start"], format="ISO8601", errors="coerce") 
            st.line_chart(ddf.set_index("period_start")[
                ["kcal", "protein_g", "fat_g", "carbs_g"]
            ])

    with c2:
        st.subheader("Сводка БЖУ - по неделям")
        weekly = requests.get(f"{API}/clients/{client_id}/summary/weekly", headers=HDR).json()
        wdf = pd.DataFrame(weekly)
        if not wdf.empty:
            wdf["period_start"] = pd.to_datetime(wdf["period_start"], format="ISO8601", errors="coerce") 
            st.line_chart(wdf.set_index("period_start")[
                ["kcal", "protein_g", "fat_g", "carbs_g"]
            ])

    st.markdown("---")

    st.subheader("Частые микронутриенты (топ-10 по употреблению)")
    micro = requests.get(f"{API}/clients/{client_id}/micro/top", headers=HDR).json()
    if micro:
        mdf = pd.DataFrame(micro)
        st.bar_chart(mdf.set_index("name_amount")["count"])

    st.markdown("---")

    st.subheader("Жиры и клетчатка - по дням")
    if not edf.empty:
        edf2 = edf[(edf["period_start"].dt.date >= date_from) & (edf["period_start"].dt.date <= date_to)].copy()
        if not edf2.empty:
            edf2["period_start"] = pd.to_datetime(edf2["period_start"], format="ISO8601", errors="coerce").dt.tz_localize(None)
            all_fats_cols = ["fats_total","fats_saturated","fats_mono","fats_poly","fats_trans"]
            selected_cols = [fat_options_map[name] for name in selected_fats if fat_options_map[name] in edf2.columns]
            cols_fats = [c for c in all_fats_cols if c in selected_cols]
            cols_fiber = [c for c in ["fiber_total","fiber_soluble","fiber_insoluble"] if c in edf2.columns]
            if cols_fats:
                st.line_chart(edf2.set_index("period_start")[cols_fats])
            if cols_fiber:
                st.line_chart(edf2.set_index("period_start")[cols_fiber])
            om_cols = [c for c in ["omega6","omega3"] if c in edf2.columns and ("Омега‑6" in selected_fats or "Омега‑3" in selected_fats)]
            if om_cols:
                st.line_chart(edf2.set_index("period_start")[om_cols])
            if "omega_ratio_num" in edf2.columns:
                st.line_chart(edf2.set_index("period_start")[["omega_ratio_num"]])

    st.subheader("Жиры и клетчатка - по неделям")
    try:
        ex_weekly = requests.get(f"{API}/clients/{client_id}/extras/weekly", headers=HDR).json()
        ewf = pd.DataFrame(ex_weekly)
    except Exception:
        ewf = pd.DataFrame()
    if not ewf.empty:
        ewf["period_start"] = pd.to_datetime(ewf["period_start"], format="ISO8601", errors="coerce").dt.tz_localize(None)
        ewf2 = ewf[(ewf["period_start"].dt.date >= date_from) & (ewf["period_start"].dt.date <= date_to)].copy()
        if not ewf2.empty:
            all_fats_cols_w = ["fats_total","fats_saturated","fats_mono","fats_poly","fats_trans"]
            selected_cols_w = [fat_options_map[name] for name in selected_fats if fat_options_map[name] in ewf2.columns]
            cols_fats_w = [c for c in all_fats_cols_w if c in selected_cols_w]
            cols_fiber_w = [c for c in ["fiber_total","fiber_soluble","fiber_insoluble"] if c in ewf2.columns]
            if cols_fats_w:
                st.line_chart(ewf2.set_index("period_start")[cols_fats_w])
            if cols_fiber_w:
                st.line_chart(ewf2.set_index("period_start")[cols_fiber_w])
            om_cols_w = [c for c in ["omega6","omega3"] if c in ewf2.columns and ("Омега‑6" in selected_fats or "Омега‑3" in selected_fats)]
            if om_cols_w:
                st.line_chart(ewf2.set_index("period_start")[om_cols_w])
            if "omega_ratio_num" in ewf2.columns:
                st.line_chart(ewf2.set_index("period_start")[["omega_ratio_num"]])


if __name__ == "__main__":
    run_app()
