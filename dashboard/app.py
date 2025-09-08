import os, json, requests, pandas as pd, streamlit as st

API = os.getenv("ADMIN_API_BASE", "http://localhost:8000")
KEY = os.getenv("ADMIN_API_KEY", "supersecret")
HDR = {"x-api-key": KEY}

st.set_page_config(page_title="Nutrios Admin", page_icon="🥗", layout="wide")
st.title("🥗 Nutrios — админка нутрициолога")

# --- helpers ---

def _safe_image_path(img_path: str) -> str | None:
    if not img_path:
        return None
    img_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "downloads"))
    full_path = img_path if os.path.isabs(img_path) else os.path.join(img_dir, img_path)
    return full_path if os.path.exists(full_path) else None


def _highlight_selected(s: pd.Series, selected_id: int | None):
    color = "background-color: #2b6cb0; color: white;"  # blue highlight
    return [color if (selected_id is not None and s.get("id") == selected_id) else "" for _ in s]


def _pretty(obj):
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


# --- data loading with basic error handling ---
try:
    clients = requests.get(f"{API}/clients", headers=HDR, timeout=10).json()
except Exception as e:
    st.error(f"Не удалось подключиться к API: {e}")
    st.stop()

# --- sidebar: client + filters ---
with st.sidebar:
    st.header("Фильтры")
    if not clients:
        st.info("Пока нет данных. Отправьте блюдо через бот.")
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
        st.info("История пуста у выбранного клиента.")
        st.stop()

    df = pd.DataFrame(meals)
    df["captured_at"] = pd.to_datetime(df["captured_at"])

    # Date range
    dmin, dmax = df["captured_at"].min().date(), df["captured_at"].max().date()
    date_from, date_to = st.date_input("Период", value=(dmin, dmax), min_value=dmin, max_value=dmax)

    # Text search
    q = st.text_input("Поиск в названии", "")

    # Flags filter (any flag present true)
    col1, col2 = st.columns(2)
    with col1:
        f1 = st.checkbox("Вегетарианское", value=False, key="vegetarian")
        f2 = st.checkbox("Веганское", value=False, key="vegan")
    with col2:
        f3 = st.checkbox("Без глютена", value=False, key="glutenfree")
        f4 = st.checkbox("Без лактозы", value=False, key="lactosefree")

    st.markdown("---")
    st.caption("Подсказка: в галерее нажмите ‘Выбрать’ чтобы подсветить блюдо в таблице.")

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

# --- main layout: gallery + table/details ---
left, right = st.columns([2, 3], gap="large")

if "selected_meal_id" not in st.session_state:
    st.session_state.selected_meal_id = None

with left:
    st.subheader("Галерея за период")
    imgs = []
    for _, r in df_f.iterrows():
        p = _safe_image_path(r.get("image_path"))
        if p:
            imgs.append((r, p))

    if not imgs:
        st.info("Нет фото в выбранном периоде или фото отсутствуют у блюд.")
    else:
        # grid 3xN
        ncols = 3
        rows = [imgs[i:i + ncols] for i in range(0, len(imgs), ncols)]
        for row in rows:
            cols = st.columns(ncols)
            for (r, p), col in zip(row, cols):
                with col:
                    st.image(p, caption=None, use_column_width=True)
                    st.caption(f"{r['captured_at'].strftime('%Y-%m-%d %H:%M')} • {r['title']}")
                    if st.button("Выбрать", key=f"sel_{r['id']}"):
                        st.session_state.selected_meal_id = r["id"]

with right:
    st.subheader("История приёмов пищи")

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
                st.markdown(
                    f"**{r['title']}**\n\n"
                    f"{r['captured_at'].strftime('%Y-%m-%d %H:%M')} • {r['portion_g']} г\n\n"
                    f"Ккал: {r['kcal']} • Б: {r['protein_g']} г • Ж: {r['fat_g']} г • У: {r['carbs_g']} г\n\n"
                )
                st.caption("Флаги: " + _pretty(r.get("flags")))
                if r.get("micronutrients"):
                    st.caption("Микроэлементы: " + ", ".join(r["micronutrients"]))
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

c1, c2 = st.columns(2)
with c1:
    st.subheader("Сумма БЖУ — по дням")
    daily = requests.get(f"{API}/clients/{client_id}/summary/daily", headers=HDR).json()
    ddf = pd.DataFrame(daily)
    if not ddf.empty:
        ddf["period_start"] = pd.to_datetime(ddf["period_start"])
        st.line_chart(ddf.set_index("period_start")[
            ["kcal", "protein_g", "fat_g", "carbs_g"]
        ])

with c2:
    st.subheader("Сумма БЖУ — по неделям")
    weekly = requests.get(f"{API}/clients/{client_id}/summary/weekly", headers=HDR).json()
    wdf = pd.DataFrame(weekly)
    if not wdf.empty:
        wdf["period_start"] = pd.to_datetime(wdf["period_start"])
        st.line_chart(wdf.set_index("period_start")[
            ["kcal", "protein_g", "fat_g", "carbs_g"]
        ])

st.markdown("---")

st.subheader("Частые микроэлементы (топ-10 по упоминаниям)")
micro = requests.get(f"{API}/clients/{client_id}/micro/top", headers=HDR).json()
if micro:
    mdf = pd.DataFrame(micro)
    st.bar_chart(mdf.set_index("name_amount")["count"])
