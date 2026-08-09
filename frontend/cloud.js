/* ChordFinder data source.
 *
 * The same pages run in two places:
 *   - on this PC   -> serve.py's JSON API, everything editable
 *   - on Firebase  -> Firestore, signed in, read-only practice view
 *
 * Which one is decided at load time by probing the local API, so no build
 * step or separate copy of the app is needed.
 */
window.DS = {
  cloud: false,
  readOnly: false,
  user: null,
  _index: null,

  async init() {
    try {
      const r = await fetch("/api/songs", { cache: "no-store" });
      if (r.ok) return;                      // local mode: full features
    } catch (_) { /* no local server -> hosted */ }
    this.cloud = true;
    this.readOnly = true;
    await this._initFirebase();
  },

  async listSongs(detail) {
    if (!this.cloud) {
      const r = await fetch(detail ? "/api/songs?detail=1" : "/api/songs",
                            { cache: "no-store" });
      return r.json();
    }
    if (!this._index) {
      let snap;
      try {
        snap = await this._db.collection("library").doc("index").get();
      } catch (e) {
        if (e.code === "permission-denied") this._denied();
        throw e;
      }
      this._index = ((snap.data() || {}).songs || []).map(s => ({ ...s, ready: true }));
    }
    return this._index;
  },

  get isOwner() {
    return !!(this.user && window.CF_OWNER_EMAIL &&
              this.user.email === window.CF_OWNER_EMAIL);
  },

  // Full song document - the extra fields the chord-sheet import needs.
  async loadSongDoc(videoId) {
    const snap = await this._db.collection("songs").doc(videoId).get();
    if (!snap.exists) throw new Error("song not published");
    return snap.data();
  },

  // Save a chord-sheet import done on this device. Firestore rules allow
  // this for the owner only; publish.py pulls it back to the PC afterwards.
  async saveImport(videoId, segments, combined) {
    const chords = [...new Set(segments.map(s => s.chord))].sort();
    await this._db.collection("songs").doc(videoId).update({
      chord_segments: segments,
      words: combined,
      chords,
      imported: true,
      edited_in_cloud: true,
    });
  },

  async loadWords(videoId) {
    if (!this.cloud) {
      const r = await fetch(`../songs/${videoId}/combined.json`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }
    const snap = await this._db.collection("songs").doc(videoId).get();
    if (!snap.exists) throw new Error("song not published");
    return (snap.data() || {}).words || [];
  },

  /* ---- Firebase (loaded only when hosted) ---- */

  async _script(src) {
    return new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = src; s.onload = res; s.onerror = () => rej(new Error("blocked: " + src));
      document.head.appendChild(s);
    });
  },

  async _initFirebase() {
    if (!window.FIREBASE_CONFIG || /REPLACE/.test(JSON.stringify(window.FIREBASE_CONFIG)))
      throw new Error("frontend/firebase-config.js is missing its project settings");
    const V = "10.12.2", base = `https://www.gstatic.com/firebasejs/${V}/firebase-`;
    for (const m of ["app-compat", "auth-compat", "firestore-compat"])
      await this._script(`${base}${m}.js`);
    firebase.initializeApp(window.FIREBASE_CONFIG);
    this._db = firebase.firestore();
    this.user = await this._signIn();
  },

  _signIn() {
    // A redirect sign-in finishes here after the page reloads.
    firebase.auth().getRedirectResult().catch(e => this._gateError(e.message));
    return new Promise(resolve => {
      firebase.auth().onAuthStateChanged(u => {
        if (u) { this._gate(false); resolve(u); }
        else this._gate(true);
      });
    });
  },

  // iOS Safari blocks OAuth popups in some configurations (and always when
  // launched from the home screen), so fall back to a full-page redirect.
  async _startSignIn() {
    const provider = new firebase.auth.GoogleAuthProvider();
    const standalone = window.navigator.standalone ||
                       matchMedia("(display-mode: standalone)").matches;
    if (standalone) return firebase.auth().signInWithRedirect(provider);
    try {
      await firebase.auth().signInWithPopup(provider);
    } catch (e) {
      if (["auth/popup-blocked", "auth/popup-closed-by-user",
           "auth/operation-not-supported-in-this-environment",
           "auth/cancelled-popup-request"].includes(e.code))
        return firebase.auth().signInWithRedirect(provider);
      throw e;
    }
  },

  _gateError(msg) {
    const p = document.getElementById("cf-gate-msg");
    if (p) p.textContent = msg;
  },

  // Signed in, but with an account that isn't in firestore.rules.
  _denied() {
    this._gate(true);
    const email = (firebase.auth().currentUser || {}).email || "that account";
    this._gateError(`${email} can't read this library. Google accounts with a ` +
                    `verified email address are allowed - try signing in again ` +
                    `with a different account.`);
    const gate = document.getElementById("cf-gate");
    if (gate && !document.getElementById("cf-signout")) {
      const out = document.createElement("button");
      out.id = "cf-signout";
      out.textContent = "Sign out";
      out.style.cssText = "background:none;color:#7a7f8f;border:1px solid #2a2d38;" +
        "border-radius:8px;padding:0.4rem 1rem;font:inherit;cursor:pointer";
      out.onclick = () => firebase.auth().signOut().then(() => location.reload());
      gate.appendChild(out);
    }
  },

  // Minimal sign-in overlay; Firestore rules are what actually protect the
  // song data, this just gets the user an identity to present.
  _gate(show) {
    let el = document.getElementById("cf-gate");
    if (!show) { if (el) el.remove(); return; }
    if (el) return;
    el = document.createElement("div");
    el.id = "cf-gate";
    el.style.cssText = "position:fixed;inset:0;z-index:200;background:#0f1014;" +
      "display:flex;flex-direction:column;align-items:center;justify-content:center;" +
      "gap:1rem;font-family:Inter,system-ui,sans-serif;color:#e2e4ea;text-align:center";
    const h = document.createElement("div");
    h.style.cssText = "font-weight:800;font-size:1.3rem;color:#ffb454";
    h.textContent = "ChordFinder";
    const p = document.createElement("div");
    p.id = "cf-gate-msg";
    p.style.cssText = "color:#7a7f8f;font-size:0.9rem;max-width:22rem;padding:0 1rem";
    p.textContent = "Sign in with the Google account that owns this library.";
    const btn = document.createElement("button");
    btn.textContent = "Sign in with Google";
    btn.style.cssText = "background:#ffb454;color:#14151a;border:0;border-radius:8px;" +
      "padding:0.6rem 1.2rem;font:inherit;font-weight:700;cursor:pointer";
    btn.onclick = () => this._startSignIn().catch(e => this._gateError(e.message));
    el.append(h, p, btn);
    document.body.appendChild(el);
  },
};
