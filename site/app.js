/* ── Amsterdam Events Calendar — Client App ──────────────────── */
'use strict';

const supabaseUrl = 'https://ktewpglahibpymvshwra.supabase.co';
const supabaseKey = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt0ZXdwZ2xhaGlicHltdnNod3JhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgyMzE0NTQsImV4cCI6MjA5MzgwNzQ1NH0.uOxMdL9JMMl3CMG6x0FRy8lgXQ5B6Me1JJxGV4kQBgg';
const supabaseClient = window.supabase ? window.supabase.createClient(supabaseUrl, supabaseKey) : null;

const SOURCE_LABELS = {
    melkweg: 'Melkweg', amsterdam_alt: 'Amsterdam Alternative',
    ra: 'Resident Advisor', paradiso: 'Paradiso', murmur: 'Murmur',
    museumkaart: 'Museumkaart', gallery_viewer: 'Gallery Viewer',
    concertgebouw: 'Concertgebouw', muziekgebouw: 'Muziekgebouw',
    sitp: 'Space is the Place', splendor: 'Splendor',
};
const TYPE_EMOJI = {
    Concert: '🎸', Club: '🎧', Film: '🎬', Festival: '🎪', Expositie: '🖼️',
};

/* ── Category definitions ─────────────────────────────────────── */
const CATEGORIES = {
    art:      { sources: ['museumkaart', 'gallery_viewer'], venues: [], genres: [] },
    classica: { sources: ['concertgebouw', 'muziekgebouw', 'splendor'], venues: ['Concertgebouw', 'Muziekgebouw', 'De Duif', 'Splendor'], genres: ['Classical'] },
    jazz:     { sources: ['sitp'], venues: ['Bimhuis'], genres: ['Jazz'] },
};
const MONTHS = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December',
];

/* ── State ────────────────────────────────────────────────────── */
const state = {
    raw: [],            // all events from JSON
    venues: [],
    genres: [],
    eventTypes: [],
    currentYear: 0,
    currentMonth: 0,    // 0-indexed
    selectedDate: null,  // 'YYYY-MM-DD'
    filters: { venues: new Set(), genres: new Set(), types: new Set(), favOnly: false, friendsOnly: false, search: '' },
    activeCategory: null,
    favorites: new Set(),
    user: null,
    globalSavedEvents: [],
};

/* ── Init ─────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', init);

async function init() {
    loadFavorites();

    // Auth Listener
    if (supabaseClient) {
        supabaseClient.auth.getSession().then(({ data: { session } }) => {
            handleAuthChange(session);
        });
        supabaseClient.auth.onAuthStateChange((_event, session) => {
            handleAuthChange(session);
        });
    }

    const main = document.querySelector('.container');
    main.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading events…</p></div>';

    try {
        const resp = await fetch('data/events.json');
        if (!resp.ok) throw new Error(resp.status);
        const data = await resp.json();
        state.raw = data.events || [];
        state.venues = data.venues || [];
        state.genres = data.genres || [];
        state.eventTypes = data.event_types || [];

        const now = new Date();
        state.currentYear = now.getFullYear();
        state.currentMonth = now.getMonth();

        document.getElementById('gen-time').textContent =
            `Last updated: ${new Date(data.generated_at).toLocaleString('en-GB', { dateStyle: 'long', timeStyle: 'short' })}`;

        // Restore full main content
        main.innerHTML = `
            <section class="calendar-section" id="calendar-section">
                <div class="cal-nav">
                    <button id="btn-prev" class="nav-btn" type="button">◀</button>
                    <h2 id="cal-title" class="cal-title"></h2>
                    <button id="btn-next" class="nav-btn" type="button">▶</button>
                    <button id="btn-today" class="today-btn" type="button">Today</button>
                </div>
                <div class="cal-grid" id="cal-grid">
                    <div class="cal-hdr">Mon</div><div class="cal-hdr">Tue</div>
                    <div class="cal-hdr">Wed</div><div class="cal-hdr">Thu</div>
                    <div class="cal-hdr">Fri</div><div class="cal-hdr">Sat</div>
                    <div class="cal-hdr">Sun</div>
                </div>
            </section>
            <section class="day-detail" id="day-detail">
                <div class="detail-header">
                    <h3 id="detail-title">Select a day to see events</h3>
                    <span class="detail-count" id="detail-count"></span>
                </div>
                <div class="events-grid" id="events-grid"></div>
            </section>
            <section class="stats-section" id="stats-section">
                <div class="stat-card"><span class="stat-val" id="stat-total">0</span><span class="stat-lbl">Events</span></div>
                <div class="stat-card"><span class="stat-val" id="stat-venues">0</span><span class="stat-lbl">Venues</span></div>
                <div class="stat-card"><span class="stat-val" id="stat-days">0</span><span class="stat-lbl">Days</span></div>
                <div class="stat-card has-tooltip"><span class="stat-val" id="stat-matches" data-tip="Events where at least one performing artist is found in Fra's Last.fm library or taste profile.">0</span><span class="stat-lbl">Matches</span></div>
                <div class="stat-card has-tooltip"><span class="stat-val" id="stat-sources" data-tip="Number of distinct platforms and venues actively feeding this calendar (e.g. Melkweg, Resident Advisor, Concertgebouw…).">0</span><span class="stat-lbl">Sources</span></div>
            </section>`;

        populateFilters();
        bindEvents();
        renderCalendar();
        updateStats();
    } catch (err) {
        main.innerHTML = `<div class="empty-state"><div class="emoji">📡</div><h4>Could not load events</h4><p>${err.message}</p></div>`;
    }
}

/* ── Filters ──────────────────────────────────────────────────── */
function populateFilters() {
    fillMenu('menu-venue', state.venues, state.filters.venues);
    fillMenu('menu-genre', state.genres, state.filters.genres);
    fillMenu('menu-type', state.eventTypes, state.filters.types);
}

function fillMenu(menuId, items, filterSet) {
    const menu = document.getElementById(menuId);
    if (!menu) return;
    menu.innerHTML = items.map(item => {
        const checked = filterSet.has(item) ? 'checked' : '';
        return `<label class="dd-item ${checked ? 'checked' : ''}">
            <input type="checkbox" value="${esc(item)}" ${checked}> ${esc(item)}
        </label>`;
    }).join('');
}

/* ── Event Binding ────────────────────────────────────────────── */
function bindEvents() {
    // Month navigation
    document.getElementById('btn-prev')?.addEventListener('click', () => { changeMonth(-1); });
    document.getElementById('btn-next')?.addEventListener('click', () => { changeMonth(1); });
    document.getElementById('btn-today')?.addEventListener('click', goToday);

    // Dropdown toggles
    document.querySelectorAll('.dropdown-toggle').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const dd = btn.closest('.dropdown');
            const wasOpen = dd.classList.contains('open');
            closeAllDropdowns();
            if (!wasOpen) dd.classList.add('open');
        });
    });

    // Dropdown checkbox changes
    document.querySelectorAll('.dropdown-menu').forEach(menu => {
        menu.addEventListener('change', handleFilterChange);
    });

    // Category buttons
    document.querySelectorAll('.category-btn').forEach(btn => {
        btn.addEventListener('click', () => toggleCategory(btn.dataset.category));
    });

    // Favorites toggle
    document.getElementById('btn-favorites')?.addEventListener('click', () => {
        state.filters.favOnly = !state.filters.favOnly;
        document.getElementById('btn-favorites').classList.toggle('active', state.filters.favOnly);
        onFiltersChanged();
    });

    // Friends' Picks toggle
    document.getElementById('btn-friends')?.addEventListener('click', () => {
        if (!state.user) {
            alert("Please log in to see what your friends are saving!");
            return;
        }
        state.filters.friendsOnly = !state.filters.friendsOnly;
        document.getElementById('btn-friends').classList.toggle('active', state.filters.friendsOnly);
        onFiltersChanged();
    });

    // Auth & Modal events
    document.getElementById('btn-login')?.addEventListener('click', () => {
        document.getElementById('login-modal').classList.remove('hidden');
        document.getElementById('login-message').textContent = '';
    });
    document.getElementById('btn-close-modal')?.addEventListener('click', () => {
        document.getElementById('login-modal').classList.add('hidden');
    });
    document.getElementById('btn-logout')?.addEventListener('click', async () => {
        if (supabaseClient) await supabaseClient.auth.signOut();
    });
    
    document.getElementById('btn-google-login')?.addEventListener('click', async () => {
        if (supabaseClient) await supabaseClient.auth.signInWithOAuth({ provider: 'google' });
    });
    document.getElementById('btn-apple-login')?.addEventListener('click', async () => {
        if (supabaseClient) await supabaseClient.auth.signInWithOAuth({ provider: 'apple' });
    });
    
    document.getElementById('magic-link-form')?.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!supabaseClient) return;
        const email = document.getElementById('login-email').value;
        const msgEl = document.getElementById('login-message');
        
        msgEl.textContent = 'Sending...';
        msgEl.className = 'login-message';
        const { error } = await supabaseClient.auth.signInWithOtp({ email });
        
        if (error) {
            msgEl.textContent = error.message;
            msgEl.classList.add('error');
        } else {
            msgEl.textContent = 'Check your email for the login link!';
            document.getElementById('login-email').value = '';
        }
    });

    // Clear filters
    document.getElementById('btn-clear')?.addEventListener('click', clearFilters);

    // Search
    let debounce;
    document.getElementById('search-input')?.addEventListener('input', (e) => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            state.filters.search = e.target.value.trim().toLowerCase();
            onFiltersChanged();
        }, 200);
    });

    // Close dropdowns on outside click
    document.addEventListener('click', closeAllDropdowns);
}

function handleFilterChange(e) {
    if (e.target.type !== 'checkbox') return;
    const menu = e.target.closest('.dropdown-menu');
    const dd = e.target.closest('.dropdown');
    const ddId = dd.id;

    let filterSet;
    if (ddId === 'dd-venue') filterSet = state.filters.venues;
    else if (ddId === 'dd-genre') filterSet = state.filters.genres;
    else if (ddId === 'dd-type') filterSet = state.filters.types;
    else return;

    if (e.target.checked) filterSet.add(e.target.value);
    else filterSet.delete(e.target.value);

    // Update checked styling
    e.target.closest('.dd-item').classList.toggle('checked', e.target.checked);

    // Update badge
    const badge = dd.querySelector('.dd-badge');
    if (badge) {
        badge.textContent = filterSet.size;
        badge.classList.toggle('visible', filterSet.size > 0);
    }

    // Manual filter change deactivates any active category
    deactivateCategory();

    onFiltersChanged();
}

function closeAllDropdowns() {
    document.querySelectorAll('.dropdown.open').forEach(dd => dd.classList.remove('open'));
}

function clearFilters() {
    state.filters.venues.clear();
    state.filters.genres.clear();
    state.filters.types.clear();
    state.filters.favOnly = false;
    state.filters.friendsOnly = false;
    state.filters.search = '';
    document.getElementById('search-input').value = '';
    document.getElementById('btn-favorites')?.classList.remove('active');
    document.getElementById('btn-friends')?.classList.remove('active');
    document.querySelectorAll('.dd-badge').forEach(b => { b.classList.remove('visible'); b.textContent = '0'; });
    document.querySelectorAll('.dd-item input').forEach(cb => { cb.checked = false; });
    document.querySelectorAll('.dd-item').forEach(item => item.classList.remove('checked'));
    deactivateCategory();
    onFiltersChanged();
}

function onFiltersChanged() {
    const hasFilters = state.filters.venues.size || state.filters.genres.size ||
        state.filters.types.size || state.filters.favOnly || state.filters.friendsOnly || state.filters.search ||
        state.activeCategory;
    document.getElementById('btn-clear')?.classList.toggle('hidden', !hasFilters);
    renderCalendar();
    if (state.selectedDate) renderDayDetail(state.selectedDate);
    updateStats();
}

/* ── Category Toggle ──────────────────────────────────────────── */
function toggleCategory(catKey) {
    if (state.activeCategory === catKey) {
        deactivateCategory();
    } else {
        deactivateCategory();
        state.activeCategory = catKey;
        document.getElementById(`btn-cat-${catKey}`)?.classList.add('active');
    }
    onFiltersChanged();
}

function deactivateCategory() {
    if (state.activeCategory) {
        document.getElementById(`btn-cat-${state.activeCategory}`)?.classList.remove('active');
    }
    state.activeCategory = null;
}

/* ── Filtering Logic ──────────────────────────────────────────── */
function matchesCategory(ev, catDef) {
    if (catDef.sources.length && catDef.sources.includes(ev.source)) return true;
    if (catDef.venues.length && catDef.venues.some(v => ev.venue && ev.venue.toLowerCase().includes(v.toLowerCase()))) return true;
    if (catDef.genres.length && ev.genres.some(g => catDef.genres.some(cg => g.toLowerCase().includes(cg.toLowerCase())))) return true;
    return false;
}

function getFiltered() {
    return state.raw.filter(ev => {
        // Category filter (overrides venue/genre/type dropdowns)
        if (state.activeCategory) {
            const catDef = CATEGORIES[state.activeCategory];
            if (catDef && !matchesCategory(ev, catDef)) return false;
        } else {
            // Standard dropdown filters only apply when no category is active
            if (state.filters.venues.size && !state.filters.venues.has(ev.venue)) return false;
            if (state.filters.genres.size && !ev.genres.some(g => state.filters.genres.has(g))) return false;
            if (state.filters.types.size && !state.filters.types.has(ev.event_type)) return false;
        }
        if (state.filters.favOnly && !state.favorites.has(ev.id)) return false;
        if (state.filters.friendsOnly) {
            const hasFriendSave = state.globalSavedEvents.some(s => s.event_id === ev.id && s.user_id !== state.user?.id);
            if (!hasFriendSave) return false;
        }
        if (state.filters.search) {
            const q = state.filters.search;
            const haystack = (ev.title + ' ' + ev.artists.join(' ') + ' ' + ev.venue).toLowerCase();
            if (!haystack.includes(q)) return false;
        }
        return true;
    });
}

function eventsForDate(dateStr, filtered) {
    return filtered.filter(ev => ev.date && ev.date.startsWith(dateStr));
}

/* ── Calendar Rendering ───────────────────────────────────────── */
function renderCalendar() {
    const grid = document.getElementById('cal-grid');
    if (!grid) return;

    // Remove old day cells (keep 7 header cells)
    while (grid.children.length > 7) grid.removeChild(grid.lastChild);

    const yr = state.currentYear, mo = state.currentMonth;
    document.getElementById('cal-title').textContent = `${MONTHS[mo]} ${yr}`;

    const firstDay = new Date(yr, mo, 1).getDay();
    const offset = (firstDay + 6) % 7; // Mon-first
    const daysInMonth = new Date(yr, mo + 1, 0).getDate();
    const todayStr = toDateStr(new Date());
    const filtered = getFiltered();

    // Empty cells before 1st
    for (let i = 0; i < offset; i++) grid.appendChild(makeCell('', [], true));

    // Day cells
    for (let d = 1; d <= daysInMonth; d++) {
        const ds = `${yr}-${pad(mo + 1)}-${pad(d)}`;
        const dayEvts = eventsForDate(ds, filtered);
        grid.appendChild(makeCell(d, dayEvts, false, ds, ds === todayStr, ds === state.selectedDate));
    }

    // Fill remaining to complete grid
    const total = offset + daysInMonth;
    const rem = (7 - (total % 7)) % 7;
    for (let i = 0; i < rem; i++) grid.appendChild(makeCell('', [], true));
}

function makeCell(day, events, isEmpty, dateStr, isToday, isSelected) {
    const cell = document.createElement('div');
    cell.className = 'cal-cell';
    if (isEmpty) { cell.classList.add('empty'); cell.innerHTML = '<span class="cal-day"></span>'; return cell; }
    if (isToday) cell.classList.add('today');
    if (isSelected) cell.classList.add('selected');

    let dotsHtml = '';
    if (events.length > 0) {
        const maxDots = 5;
        const shown = events.slice(0, maxDots);
        dotsHtml = '<div class="cal-dots">' +
            shown.map(ev => `<span class="cal-dot ${dotClass(ev)}"></span>`).join('') +
            '</div>';
        if (events.length > maxDots) {
            dotsHtml += `<span class="cal-count">+${events.length - maxDots}</span>`;
        }
    }

    cell.innerHTML = `<span class="cal-day">${day}</span>${dotsHtml}`;
    cell.addEventListener('click', () => selectDate(dateStr));
    return cell;
}

function dotClass(ev) {
    if (ev.match) return ev.match.type === 'Library Match' ? 'match-library' : 'match-discovery';
    if (ev.tickets_status === 'sold_out' || ev.tickets_status === 'cancelled') return 'sold-out';
    if (ev.tickets_status === 'free') return 'free';
    if (ev.event_type === 'Club') return 'club';
    if (ev.event_type === 'Concert') return 'concert';
    return 'default';
}

function selectDate(dateStr) {
    state.selectedDate = dateStr;
    renderCalendar(); // re-render to update selected highlight
    renderDayDetail(dateStr);
    document.getElementById('day-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function changeMonth(delta) {
    state.currentMonth += delta;
    if (state.currentMonth > 11) { state.currentMonth = 0; state.currentYear++; }
    if (state.currentMonth < 0) { state.currentMonth = 11; state.currentYear--; }
    state.selectedDate = null;
    renderCalendar();
    clearDayDetail();
}

function goToday() {
    const now = new Date();
    state.currentYear = now.getFullYear();
    state.currentMonth = now.getMonth();
    const todayStr = toDateStr(now);
    state.selectedDate = todayStr;
    renderCalendar();
    renderDayDetail(todayStr);
}

/* ── Day Detail Rendering ─────────────────────────────────────── */
function renderDayDetail(dateStr) {
    const filtered = getFiltered();
    const dayEvts = eventsForDate(dateStr, filtered);
    const dateObj = new Date(dateStr + 'T00:00:00');
    const dayName = dateObj.toLocaleDateString('en-GB', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });

    document.getElementById('detail-title').textContent = dayName;
    document.getElementById('detail-count').textContent = dayEvts.length
        ? `${dayEvts.length} event${dayEvts.length !== 1 ? 's' : ''}`
        : '';

    const grid = document.getElementById('events-grid');
    if (!dayEvts.length) {
        grid.innerHTML = '<div class="empty-state"><div class="emoji">🌙</div><h4>No events this day</h4><p>Try adjusting your filters or pick another date.</p></div>';
        return;
    }

    grid.innerHTML = dayEvts.map(ev => cardHTML(ev)).join('');

    // Bind card actions
    grid.querySelectorAll('.fav-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleFavorite(btn.dataset.id);
            btn.classList.toggle('active', state.favorites.has(btn.dataset.id));
            btn.textContent = state.favorites.has(btn.dataset.id) ? '★ Saved' : '☆ Save';
        });
    });

    grid.querySelectorAll('.cal-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const ev = state.raw.find(x => x.id === btn.dataset.id);
            if (ev) downloadICS(ev);
        });
    });

    // Re-trigger animation
    const section = document.getElementById('day-detail');
    section.style.animation = 'none';
    section.offsetHeight; // reflow
    section.style.animation = '';
}

function clearDayDetail() {
    document.getElementById('detail-title').textContent = 'Select a day to see events';
    document.getElementById('detail-count').textContent = '';
    document.getElementById('events-grid').innerHTML = '';
}

function cardHTML(ev) {
    const emoji = TYPE_EMOJI[ev.event_type] || '📌';
    const srcLabel = SOURCE_LABELS[ev.source] || ev.source;
    const isFav = state.favorites.has(ev.id);

    let badgeHtml = '';
    if (ev.tickets_status === 'sold_out') badgeHtml = '<span class="badge sold-out">Sold Out</span>';
    else if (ev.tickets_status === 'cancelled') badgeHtml = '<span class="badge cancelled">Cancelled</span>';
    else if (ev.tickets_status === 'free') badgeHtml = '<span class="badge free">Free</span>';

    let matchHtml = '';
    if (ev.match) {
        const cls = ev.match.type === 'Library Match' ? 'library' : 'discovery';
        const icon = ev.match.type === 'Library Match' ? '🔥' : '✨';
        matchHtml = `<div class="match-badge ${cls}">${icon} ${esc(ev.match.reason)}</div>`;
    }

    const artistsHtml = ev.artists?.length
        ? `<div class="artists">${esc(ev.artists.slice(0, 6).join(' · '))}</div>` : '';

    const genresHtml = ev.genres?.length
        ? `<div class="genres">${ev.genres.slice(0, 4).map(g => `<span class="genre-tag">${esc(g)}</span>`).join('')}</div>` : '';

    const venueHtml = ev.venue ? `<span class="venue">${esc(ev.venue)}</span>` : '';

    const time = ev.date ? new Date(ev.date).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '';

    let friendsHtml = '';
    if (state.user && state.globalSavedEvents.length > 0) {
        const friendsWhoSaved = state.globalSavedEvents.filter(s => s.event_id === ev.id && s.user_id !== state.user.id);
        if (friendsWhoSaved.length > 0) {
            const uniqueFriends = [];
            const seenUsers = new Set();
            for (const s of friendsWhoSaved) {
                if (!seenUsers.has(s.user_id)) {
                    seenUsers.add(s.user_id);
                    uniqueFriends.push(s);
                }
            }
            friendsHtml = `<div class="friends-saves" title="Saved by friends">` +
                uniqueFriends.slice(0, 3).map(f => {
                    const initial = (f.user_name || 'U').charAt(0).toUpperCase();
                    return `<div class="friend-avatar">${esc(initial)}</div>`;
                }).join('') +
                (uniqueFriends.length > 3 ? `<div class="friend-avatar">+${uniqueFriends.length - 3}</div>` : '') +
                `</div>`;
        }
    }

    return `<div class="event-card">
        <a href="${esc(ev.source_url)}" target="_blank" rel="noopener" class="card-link">
            <div class="card-top">
                <span class="event-type">${emoji} ${esc(ev.event_type)}${time ? ' · ' + time : ''}</span>
                ${badgeHtml}
            </div>
            ${matchHtml}
            <h3 class="event-title">${esc(ev.title)}</h3>
            ${artistsHtml}
            ${genresHtml}
            <div class="card-footer">
                ${venueHtml}
                <span class="source">${esc(srcLabel)}</span>
                ${friendsHtml}
            </div>
        </a>
        <div class="card-actions">
            <button class="cal-btn" data-id="${ev.id}" type="button">📅 Calendar</button>
            <button class="fav-btn ${isFav ? 'active' : ''}" data-id="${ev.id}" type="button">${isFav ? '★ Saved' : '☆ Save'}</button>
        </div>
    </div>`;
}

/* ── Stats ────────────────────────────────────────────────────── */
function updateStats() {
    const filtered = getFiltered();
    const dates = new Set(filtered.map(ev => ev.date?.split('T')[0]).filter(Boolean));
    const vSet = new Set(filtered.map(ev => ev.venue).filter(Boolean));
    const matches = filtered.filter(ev => ev.match).length;
    const srcSet = new Set(state.raw.map(ev => ev.source).filter(Boolean));

    const el = (id) => document.getElementById(id);
    if (el('stat-total')) el('stat-total').textContent = filtered.length;
    if (el('stat-venues')) el('stat-venues').textContent = vSet.size;
    if (el('stat-days')) el('stat-days').textContent = dates.size;
    if (el('stat-matches')) el('stat-matches').textContent = matches;
    if (el('stat-sources')) {
        el('stat-sources').textContent = srcSet.size;
        const srcArray = Array.from(srcSet).map(s => SOURCE_LABELS[s] || s).sort();
        let srcText = 'Active sources feeding this calendar:\n\n';
        if (srcArray.length <= 6) {
            srcText += srcArray.map(s => '•\xa0' + s).join('\n');
        } else {
            srcText += srcArray.slice(0, 5).map(s => '•\xa0' + s).join('\n') + `\n\n... and ${srcArray.length - 5} more.`;
        }
        el('stat-sources').setAttribute('data-tip', srcText);
    }
}

/* ── Favorites ────────────────────────────────────────────────── */
function loadFavorites() {
    try {
        const saved = JSON.parse(localStorage.getItem('ams_events_favs') || '[]');
        state.favorites = new Set(saved);
    } catch { state.favorites = new Set(); }
}

function saveFavorites() {
    localStorage.setItem('ams_events_favs', JSON.stringify([...state.favorites]));
}

async function toggleFavorite(id) {
    const isAdding = !state.favorites.has(id);
    
    if (isAdding) {
        state.favorites.add(id);
    } else {
        state.favorites.delete(id);
    }
    saveFavorites();
    
    if (state.user && supabaseClient) {
        if (isAdding) {
            const userName = state.user.email ? state.user.email.split('@')[0] : 'User';
            await supabaseClient.from('saved_events').insert([{ 
                user_id: state.user.id, 
                user_name: userName, 
                event_id: id 
            }]);
            state.globalSavedEvents.push({ user_id: state.user.id, user_name: userName, event_id: id });
        } else {
            await supabaseClient.from('saved_events').delete().match({ user_id: state.user.id, event_id: id });
            state.globalSavedEvents = state.globalSavedEvents.filter(s => !(s.user_id === state.user.id && s.event_id === id));
        }
    }
    
    if (state.filters.favOnly || state.filters.friendsOnly) onFiltersChanged();
}

/* ── Auth Logic ───────────────────────────────────────────────── */
async function handleAuthChange(session) {
    state.user = session ? session.user : null;
    const btnLogin = document.getElementById('btn-login');
    const userProfile = document.getElementById('user-profile');
    const userAvatar = document.getElementById('user-avatar');
    
    if (state.user) {
        if (btnLogin) btnLogin.classList.add('hidden');
        if (userProfile) userProfile.classList.remove('hidden');
        const email = state.user.email || '';
        const initial = email ? email.charAt(0).toUpperCase() : 'U';
        if (userAvatar) userAvatar.textContent = initial;
        document.getElementById('login-modal')?.classList.add('hidden');
        
        await fetchSavedEvents();
    } else {
        if (btnLogin) btnLogin.classList.remove('hidden');
        if (userProfile) userProfile.classList.add('hidden');
        state.globalSavedEvents = [];
        state.filters.friendsOnly = false;
        document.getElementById('btn-friends')?.classList.remove('active');
        onFiltersChanged();
    }
}

async function fetchSavedEvents() {
    if (!supabaseClient || !state.user) return;
    try {
        const { data, error } = await supabaseClient.from('saved_events').select('*');
        if (error) throw error;
        state.globalSavedEvents = data || [];
        
        // Merge user's db saves into local favorites
        const mySaves = state.globalSavedEvents.filter(s => s.user_id === state.user.id);
        mySaves.forEach(s => state.favorites.add(s.event_id));
        saveFavorites();
        
        onFiltersChanged();
    } catch (err) {
        console.error('Error fetching saved events:', err);
    }
}

/* ── ICS Download ─────────────────────────────────────────────── */
function downloadICS(ev) {
    const start = new Date(ev.date);
    let end;
    if (ev.date_end) {
        end = new Date(ev.date_end);
    } else {
        end = new Date(start.getTime() + 3 * 3600000);
    }
    // Cap at 23:59 of event day
    const cap = new Date(start);
    cap.setHours(23, 59, 0, 0);
    if (end > cap) end = cap;
    if (end <= start) end = cap;

    const fmt = d => d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
    const icsEsc = s => (s || '').replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\n/g, '\\n');
    const loc = ev.venue ? `${ev.venue}, Amsterdam` : 'Amsterdam';

    const parts = [];
    if (ev.event_type) parts.push(ev.event_type);
    if (ev.artists?.length) parts.push('Artists: ' + ev.artists.slice(0, 6).join(', '));
    if (ev.source_url) parts.push(ev.source_url);

    const ics = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//AmsterdamEvents//Calendar//EN',
        'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'BEGIN:VEVENT',
        `DTSTART:${fmt(start)}`, `DTEND:${fmt(end)}`,
        `SUMMARY:${icsEsc(ev.title)}`, `LOCATION:${icsEsc(loc)}`,
        `DESCRIPTION:${icsEsc(parts.join(' — '))}`,
        `URL:${ev.source_url}`, `UID:${ev.id}@amsterdamevents`,
        'STATUS:CONFIRMED', 'END:VEVENT', 'END:VCALENDAR',
    ].join('\r\n');

    const blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (ev.title || 'event').replace(/[^a-zA-Z0-9 ]/g, '').slice(0, 30).trim() + '.ics';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

/* ── Helpers ──────────────────────────────────────────────────── */
function pad(n) { return String(n).padStart(2, '0'); }
function toDateStr(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}
