import os, requests, pandas as pd, streamlit as st

API = os.getenv("ADMIN_API_BASE", "http://localhost:8000")
KEY = os.getenv("ADMIN_API_KEY", "supersecret")
HDR = {"x-api-key": KEY}

st.set_page_config(page_title="Nutrios Admin", page_icon="🥗", layout="wide")
st.title("🥗 Nutrios — админка нутрициолога")

# 1) Список клиентов
clients = requests.get(f"{API}/clients", headers=HDR).json()
left, right = st.columns([1,3])
with left:
    st.subheader("Клиенты")
    if not clients:
        st.info("Пока нет данных. Отправьте блюдо через бот.")
    options = {f'@{c["telegram_username"]} ({c["telegram_user_id"]})' if c["telegram_username"] else c["telegram_user_id"]: c["id"] for c in clients}
    choice = st.selectbox("Выберите клиента", options.keys()) if clients else None
    client_id = options.get(choice) if clients else None

with right:
    if not clients or not client_id:
        st.stop()
    st.subheader("История приёмов пищи")

    meals = requests.get(f"{API}/clients/{client_id}/meals", headers=HDR).json()
    if not meals:
        st.info("История пуста."); st.stop()

    df = pd.DataFrame(meals)
    df["captured_at"] = pd.to_datetime(df["captured_at"])
    df_show = df[["captured_at","title","portion_g","kcal","protein_g","fat_g","carbs_g","flags","micronutrients","source_type","image_path"]]

    # Галерея фото
    st.subheader("Загруженные фото")
    img_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "downloads"))
    images = []
    for img_path in df["image_path"].dropna():
        # Если путь относительный, ищем файл в downloads
        if not os.path.isabs(img_path):
            full_path = os.path.join(img_dir, img_path)
        else:
            full_path = img_path
        if os.path.exists(full_path):
            images.append(full_path)
    if images:
        for img in images:
            st.image(img, width=200)
    else:
        st.info("Нет загруженных фото для выбранного клиента.")

    st.dataframe(df_show, use_container_width=True, height=320)

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Сумма БЖУ — по дням")
        daily = requests.get(f"{API}/clients/{client_id}/summary/daily", headers=HDR).json()
        ddf = pd.DataFrame(daily)
        if not ddf.empty:
            ddf["period_start"] = pd.to_datetime(ddf["period_start"])
            st.line_chart(ddf.set_index("period_start")[["kcal","protein_g","fat_g","carbs_g"]])

    with c2:
        st.subheader("Сумма БЖУ — по неделям")
        weekly = requests.get(f"{API}/clients/{client_id}/summary/weekly", headers=HDR).json()
        wdf = pd.DataFrame(weekly)
        if not wdf.empty:
            wdf["period_start"] = pd.to_datetime(wdf["period_start"])
            st.line_chart(wdf.set_index("period_start")[["kcal","protein_g","fat_g","carbs_g"]])

    st.markdown("---")
    st.subheader("Частые микроэлементы (топ-10 по упоминаниям)")
    micro = requests.get(f"{API}/clients/{client_id}/micro/top", headers=HDR).json()
    if micro:
        mdf = pd.DataFrame(micro)
        st.bar_chart(mdf.set_index("name_amount")["count"])
