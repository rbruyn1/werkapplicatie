/* ============================================================
   update_check.js — Controleert periodiek op een nieuwe versie op
   GitHub en toont een badge in de header als die klaarstaat.

   Belangrijk: dit herstart NOOIT vanzelf. De check hieronder is
   puur informatief; enkel een expliciete klik + bevestiging in het
   popupvenster start de effectieve 'git pull' + herstart. Dat is
   bewust zo (i.p.v. start.py's bestandswatcher die soms hinderlijk
   herstart terwijl er nog een PeopleSoft-actie loopt).

   Injecteert zijn eigen <style>, dus geen wijziging nodig aan de
   losse <style>-blokken per pagina - enkel dit script includen.
   ============================================================ */
(function () {
  const CONTROLE_INTERVAL_MS = 15 * 60 * 1000; // elke 15 minuten

  const stijl = document.createElement('style');
  stijl.textContent = `
    .update-badge {
      font-family: 'IBM Plex Mono', monospace;
      font-size: 11px;
      padding: 5px 10px;
      border-radius: 2px;
      cursor: pointer;
      border: 1px solid #d9a02d;
      color: #d9a02d;
      transition: opacity .15s;
    }
    .update-badge:hover { opacity: .7; }
    .update-badge.hidden { display: none; }
    .update-badge.bezig { color: #6b7280; border-color: #6b7280; cursor: default; }
    .update-badge.bezig:hover { opacity: 1; }
  `;
  document.head.appendChild(stijl);

  let badgeEl = null;
  let laatsteStatus = null;

  function maakBadge() {
    const badge = document.createElement('span');
    badge.id = 'update-badge';
    badge.className = 'update-badge hidden';
    badge.title = 'Klik om nu bij te werken';
    badge.textContent = '🔄 Update beschikbaar';
    badge.addEventListener('click', vraagBevestiging);
    const headerRechts = document.querySelector('.header-right') || document.body;
    headerRechts.insertBefore(badge, headerRechts.firstChild);
    return badge;
  }

  async function controleer() {
    try {
      const res = await fetch('/api/update-check');
      const data = await res.json();
      if (!data.ok) return;
      laatsteStatus = data;
      if (!badgeEl) badgeEl = maakBadge();
      if (!badgeEl.classList.contains('bezig')) {
        badgeEl.classList.toggle('hidden', !data.update_beschikbaar);
      }
    } catch (err) {
      // Geen internet / git even niet beschikbaar - stil negeren,
      // volgende poging over CONTROLE_INTERVAL_MS.
    }
  }

  async function vraagBevestiging() {
    if (!laatsteStatus || !laatsteStatus.update_beschikbaar) return;
    const zeker = confirm(
      `Nieuwe versie beschikbaar (${laatsteStatus.huidig} → ${laatsteStatus.nieuwste}).\n\n` +
      `De app wordt bijgewerkt en herstart daarna zelf. Zorg dat er op deze pc ` +
      `geen lopende actie is (bv. een werkorder aan het aanmaken) voor je bevestigt.\n\n` +
      `Nu bijwerken en herstarten?`
    );
    if (!zeker) return;
    voerUpdateUit();
  }

  async function voerUpdateUit() {
    badgeEl.textContent = '⏳ Bijwerken...';
    badgeEl.classList.add('bezig');
    try {
      const res = await fetch('/api/update-nu', { method: 'POST' });
      const data = await res.json();
      if (data.ok) {
        badgeEl.textContent = '⏳ Herstart...';
        setTimeout(() => wachtOpHerstart(), 1500);
      } else {
        alert('Bijwerken mislukt: ' + (data.fout || 'onbekende fout'));
        badgeEl.textContent = '🔄 Update beschikbaar';
        badgeEl.classList.remove('bezig');
      }
    } catch (err) {
      // De verbinding valt normaal weg zodra het proces zichzelf vervangt
      // (os.execv) - dat is hier verwacht, geen echte fout.
      setTimeout(() => wachtOpHerstart(), 1500);
    }
  }

  function wachtOpHerstart(poging) {
    poging = poging || 0;
    if (poging > 30) { // ~1 minuut geprobeerd
      badgeEl.textContent = '⚠️ Herstart duurt lang — herlaad handmatig';
      return;
    }
    fetch('/api/ping', { cache: 'no-store' })
      .then(res => { if (res.ok) location.reload(); else throw new Error(); })
      .catch(() => setTimeout(() => wachtOpHerstart(poging + 1), 2000));
  }

  document.addEventListener('DOMContentLoaded', () => {
    controleer();
    setInterval(controleer, CONTROLE_INTERVAL_MS);
  });
})();
