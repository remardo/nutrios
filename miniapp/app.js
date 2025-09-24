// Nutrios MiniApp – uses Telegram WebApp API and Admin API
(function(){
  const tg = window.Telegram?.WebApp;
  if (tg) tg.ready();

  const qs = new URLSearchParams(location.search);
  const debugUser = qs.get('tg');
  let clientId = null;

  async function fetchJSON(url, opts={}){
    const headers = Object.assign({}, opts.headers||{});
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
    const res = await fetch(url, Object.assign({}, opts, { headers }));
    if (!res.ok) throw new Error(res.status+" "+res.statusText);
    return await res.json();
  }

  function userIdFromTG(){
    try{
      if (debugUser) return parseInt(debugUser,10);
      const u = tg?.initDataUnsafe?.user;
      return u?.id ? parseInt(u.id,10) : null;
    }catch(e){ return null }
  }

  function kpi(label, value, meta){
    return `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div>${meta?`<div class="meta">${meta}</div>`:''}</div>`;
  }

  function pct(v){ if (v==null) return '-'; return (v>=100?`<span class="ok">${v}%</span>`:(v>=80?`${v}%`:`<span class="warn">${v}%</span>`)); }

  function todayISO(){ return new Date().toISOString().slice(0,10); }

  function safeNumber(v){ const n = Number(v); return Number.isFinite(n) ? Math.round(n) : 0; }

  function renderChallenge(row){
    const progress = row.progress || {};
    const meta = progress.meta || row.meta || {};
    const unit = meta.unit ? ` ${meta.unit}` : '';
    const value = safeNumber(progress.value);
    const target = safeNumber(progress.target_value != null ? progress.target_value : row.target_value);
    const status = (progress.completed || row.status === 'completed') ? '✅' : (row.status === 'failed' ? '❌' : '🔥');
    const period = (row.start_date && row.end_date) ? `<div class="meta">Период: ${row.start_date} – ${row.end_date}</div>` : '';
    return `<div class="challenge-item"><div class="title">${status} ${row.name || row.code || 'Челлендж'}</div><div class="meta">Прогресс: ${value}/${target}${unit}</div>${period}</div>`;
  }

  async function resolveClient(){
    const uid = userIdFromTG();
    if (!uid) throw new Error('Не найден Telegram user id');
    // Prefer verified Telegram initData. If page opened with ?tg=... (local debug), server will validate only when enabled via env.
    const url = debugUser ? `/client/by_telegram/${uid}?tg=${uid}` : `/client/by_telegram/${uid}`;
    const row = await fetchJSON(url);
    clientId = row.id;
    return clientId;
  }

  async function loadTargets(){
    const t = await fetchJSON(`/clients/${clientId}/targets`);
    const el = document.getElementById('targets');
    el.innerHTML = [
      kpi('Калории', `${t.kcal_target} ккал`),
      kpi('Белки', `${t.protein_target_g} г`),
      kpi('Жиры', `${t.fat_target_g} г`),
      kpi('Углеводы', `${t.carbs_target_g} г`),
    ].join('');
    return t;
  }

  function pickToday(rows){
    const today = new Date().toISOString().slice(0,10);
    let chosen = rows.find(r => (r.period_start||'').startsWith(today));
    if (!chosen && rows.length) chosen = rows[rows.length-1];
    return chosen || null;
  }

  async function loadDaily(){
    const rows = await fetchJSON(`/clients/${clientId}/progress/daily`);
    const r = pickToday(rows);
    const el = document.getElementById('progressDaily');
    if (!r){ el.innerHTML = '<div class="muted">Нет данных за сегодня</div>'; return; }
    el.innerHTML = [
      kpi('Калории', Math.round(r.kcal), pct(r.kcal_pct)),
      kpi('Белки', Math.round(r.protein_g)+' г', pct(r.protein_pct)),
      kpi('Жиры', Math.round(r.fat_g)+' г', pct(r.fat_pct)),
      kpi('Углеводы', Math.round(r.carbs_g)+' г', pct(r.carbs_pct)),
    ].join('');
    try { renderDailyChart(rows); } catch(e) {}
  }

  async function loadWeekly(){
    const rows = await fetchJSON(`/clients/${clientId}/progress/weekly`);
    const r = rows.length? rows[rows.length-1] : null;
    const el = document.getElementById('progressWeekly');
    if (!r){ el.innerHTML = '<div class="muted">Нет данных за неделю</div>'; return; }
    el.innerHTML = [
      kpi('Калории', Math.round(r.kcal), pct(r.kcal_pct)),
      kpi('Белки', Math.round(r.protein_g)+' г', pct(r.protein_pct)),
      kpi('Жиры', Math.round(r.fat_g)+' г', pct(r.fat_pct)),
      kpi('Углеводы', Math.round(r.carbs_g)+' г', pct(r.carbs_pct)),
    ].join('');
    try { renderWeeklyChart(rows); } catch(e) {}
  }

  async function loadStreak(){
    const s = await fetchJSON(`/clients/${clientId}/streak`);
    const el = document.getElementById('streak');
    el.innerHTML = `Текущая серия: <b>${s.streak}</b> ${s.met_goal_7? '🔥 Цель 7 дней достигнута!' : ''}`;
  }

  async function loadChallenges(){
    if (!clientId) return;
    const listEl = document.getElementById('challengesActive');
    const suggestEl = document.getElementById('challengeSuggestion');
    try{
      const active = await fetchJSON(`/clients/${clientId}/challenges/active`);
      if (!active.length){
        listEl.innerHTML = '<div class="muted">Пока нет активных челленджей</div>';
      } else {
        listEl.innerHTML = active.map(renderChallenge).join('');
      }
      const available = await fetchJSON(`/clients/${clientId}/challenges/available`);
      const suggestion = (available||[]).find(row => !row.already_active);
      if (suggestion){
        const unit = suggestion.meta?.unit ? ` ${suggestion.meta.unit}` : '';
        const target = safeNumber(suggestion.suggested_target ?? suggestion.target_value);
        suggestEl.innerHTML = `Следующий шаг: <b>${suggestion.name || suggestion.code}</b> — цель ${target}${unit}.`;
      } else {
        suggestEl.innerHTML = '';
      }
    }catch(e){
      listEl.innerHTML = '<div class="muted">Не удалось загрузить челленджи</div>';
      suggestEl.innerHTML = '';
    }
  }

  function renderHabitStatus(row){
    const el = document.getElementById('habitStatus');
    if (!el) return;
    el.innerHTML = [
      `Вода: <b>${row.water_ml || 0}</b> мл`,
      `Овощи: <b>${row.vegetables_g || 0}</b> г`,
      `Шаги: <b>${row.steps || 0}</b>`,
      `Сладкое: <b>${row.had_sweets ? 'да' : 'нет'}</b>`,
      `Приёмов: <b>${row.logged_meals || 0}</b>`
    ].join('<br/>');
  }

  async function loadHabit(dateStr){
    if (!clientId || !dateStr) return;
    try{
      const data = await fetchJSON(`/clients/${clientId}/habits/${dateStr}`);
      const form = document.getElementById('habitForm');
      if (form){
        if (form.elements['date']) form.elements['date'].value = dateStr;
        if (form.elements['water_ml']) form.elements['water_ml'].value = data.water_ml ?? '';
        if (form.elements['steps']) form.elements['steps'].value = data.steps ?? '';
        if (form.elements['vegetables_g']) form.elements['vegetables_g'].value = data.vegetables_g ?? '';
        if (form.elements['had_sweets']) form.elements['had_sweets'].value = data.had_sweets ? 'true' : 'false';
      }
      renderHabitStatus(data);
    }catch(e){
      const status = document.getElementById('habitStatus');
      if (status) status.innerHTML = 'Не удалось загрузить данные.';
    }
  }

  async function submitHabit(ev){
    ev.preventDefault();
    if (!clientId) return;
    const form = ev.target;
    const dateStr = form.elements['date']?.value || todayISO();
    const payload = {};
    ['water_ml','steps','vegetables_g'].forEach(key => {
      const field = form.elements[key];
      if (field && field.value !== '') payload[key] = Number(field.value);
    });
    const sweets = form.elements['had_sweets']?.value;
    if (sweets === 'true' || sweets === 'false') payload.had_sweets = (sweets === 'true');
    try{
      await fetchJSON(`/clients/${clientId}/habits/${dateStr}`, {
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      await loadHabit(dateStr);
      await loadChallenges();
    }catch(e){
      const status = document.getElementById('habitStatus');
      if (status) status.innerHTML = 'Ошибка сохранения.';
    }
  }

  async function submitQuiz(ev){
    ev.preventDefault();
    const form = ev.target;
    const data = Object.fromEntries(new FormData(form).entries());
    // convert numbers
    ['age','height_cm','weight_kg'].forEach(k=>{ if (data[k] !== undefined && data[k] !== '') data[k] = Number(data[k]); });
    await fetchJSON(`/clients/${clientId}/questionnaire`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
    await loadTargets();
    await loadDaily();
    await loadWeekly();
  }

  async function editTargets(){
    const kcal = Number(prompt('Калории (ккал):','2000'));
    const p = Number(prompt('Белки (г):','100'));
    const f = Number(prompt('Жиры (г):','70'));
    const c = Number(prompt('Углеводы (г):','250'));
    if (!kcal || !p || !f || !c) return;
    await fetchJSON(`/clients/${clientId}/targets`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({kcal_target:kcal,protein_target_g:p,fat_target_g:f,carbs_target_g:c}) });
    await loadTargets(); await loadDaily(); await loadWeekly();
  }

  async function boot(){
    try{
      await resolveClient();
      await loadTargets();
      await loadDaily();
      await loadWeekly();
      await loadStreak();
      await loadTips();
      const habitDate = document.getElementById('habitDate');
      const dateValue = habitDate?.value || todayISO();
      if (habitDate && !habitDate.value) habitDate.value = dateValue;
      await loadHabit(dateValue);
      await loadChallenges();
    }catch(e){
      alert('Ошибка инициализации: '+e.message);
    }
  }

  document.getElementById('quiz').addEventListener('submit', submitQuiz);
  document.getElementById('refresh').addEventListener('click', boot);
  document.getElementById('editTargets').addEventListener('click', editTargets);
  document.getElementById('habitForm').addEventListener('submit', submitHabit);
  document.getElementById('habitDate').addEventListener('change', ev => loadHabit(ev.target.value));
  boot();

  function renderDailyChart(rows){
    const el = document.getElementById('chartDaily'); if (!el) return;
    const labels = rows.map(r => (r.period_start||'').slice(0,10));
    const data = {
      labels,
      datasets: [
        { label:'Ккал', data: rows.map(r=>r.kcal), borderColor:'#f59e0b', backgroundColor:'rgba(245,158,11,0.2)' },
        { label:'Белки', data: rows.map(r=>r.protein_g), borderColor:'#3b82f6', backgroundColor:'rgba(59,130,246,0.2)' },
        { label:'Жиры', data: rows.map(r=>r.fat_g), borderColor:'#10b981', backgroundColor:'rgba(16,185,129,0.2)' },
        { label:'Углеводы', data: rows.map(r=>r.carbs_g), borderColor:'#8b5cf6', backgroundColor:'rgba(139,92,246,0.2)' },
      ]
    };
    new Chart(el.getContext('2d'), { type:'line', data, options:{ responsive: true, plugins:{ legend:{ labels:{ color:'#e6e8ee' } } }, scales:{ x:{ ticks:{ color:'#9aa3b2' } }, y:{ ticks:{ color:'#9aa3b2' } } } } });
  }

  function renderWeeklyChart(rows){
    const el = document.getElementById('chartWeekly'); if (!el) return;
    const labels = rows.map(r => (r.period_start||'').slice(0,10));
    const data = {
      labels,
      datasets: [
        { label:'Ккал', data: rows.map(r=>r.kcal), borderColor:'#f59e0b', backgroundColor:'rgba(245,158,11,0.2)' },
        { label:'Белки', data: rows.map(r=>r.protein_g), borderColor:'#3b82f6', backgroundColor:'rgba(59,130,246,0.2)' },
        { label:'Жиры', data: rows.map(r=>r.fat_g), borderColor:'#10b981', backgroundColor:'rgba(16,185,129,0.2)' },
        { label:'Углеводы', data: rows.map(r=>r.carbs_g), borderColor:'#8b5cf6', backgroundColor:'rgba(139,92,246,0.2)' },
      ]
    };
    new Chart(el.getContext('2d'), { type:'line', data, options:{ responsive: true, plugins:{ legend:{ labels:{ color:'#e6e8ee' } } }, scales:{ x:{ ticks:{ color:'#9aa3b2' } }, y:{ ticks:{ color:'#9aa3b2' } } } } });
  }

  async function loadTips(){
    try{
      const res = await fetchJSON(`/clients/${clientId}/tips/today`);
      const div = document.createElement('div');
      div.className = 'card';
      div.innerHTML = '<div class="section-title">Подсказки</div><ul>'+ (res.tips||[]).map(t=>`<li>${t}</li>`).join('') +'</ul>';
      document.querySelector('main').appendChild(div);
    }catch(e){ /* ignore */ }
  }
})();
