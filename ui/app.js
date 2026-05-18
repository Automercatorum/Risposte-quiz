function app() {
  return {
    screen: "loading",
    busy: false,
    error: "",
    bootError: "",
    username: "",
    form: { user: "", pass: "", remember: false },
    courses: [],
    selected: [],
    progress: [],
    extracting: false,

    async waitForApi() {
      for (let i = 0; i < 100; i++) {
        if (window.pywebview?.api?.autoLogin) return true;
        await new Promise((r) => setTimeout(r, 100));
      }
      return false;
    },

    async boot() {
      try {
        const ok = await this.waitForApi();
        if (!ok) {
          this.bootError = "Bridge pywebview non disponibile dopo 10s.";
          this.screen = "error";
          return;
        }
        const res = await window.pywebview.api.autoLogin();
        if (res.firstRun) { this.screen = "login"; return; }
        if (res.ok) {
          this.username = res.username;
          this.courses = res.courses;
          this.screen = "courses";
        } else {
          this.error = res.error || "Login fallito";
          this.screen = "login";
        }
      } catch (e) {
        this.bootError = String(e?.message || e);
        this.screen = "error";
      }
    },

    async doLogin() {
      this.error = "";
      this.busy = true;
      try {
        const res = await window.pywebview.api.login(
          this.form.user, this.form.pass, this.form.remember
        );
        if (!res.ok) { this.error = res.error || "Errore"; return; }
        this.username = res.username;
        this.courses = res.courses;
        this.form.pass = "";
        this.screen = "courses";
      } catch (e) {
        this.error = String(e?.message || e);
      } finally {
        this.busy = false;
      }
    },

    async forgetAccount() {
      if (!confirm("Dimenticare l'account salvato?")) return;
      await window.pywebview.api.forgetAccount();
      this.courses = [];
      this.selected = [];
      this.username = "";
      this.form = { user: "", pass: "", remember: false };
      this.screen = "login";
    },

    async logout() {
      await window.pywebview.api.logout();
      this.courses = [];
      this.selected = [];
      this.progress = [];
      this.screen = "login";
    },

    allSelected() {
      return this.courses.length > 0 && this.selected.length === this.courses.length;
    },
    toggleAll() {
      this.selected = this.allSelected() ? [] : this.courses.map((c) => c.code);
    },

    startExtract() {
      this.progress = this.selected.map((code) => {
        const c = this.courses.find((x) => x.code === code);
        return {
          code, name: c.name, status: "queued",
          statusLabel: "in coda…", message: "",
        };
      });
      this.extracting = true;
      this.screen = "extract";
      window.pywebview.api.extract(this.selected);
    },

    onProgress(evt) {
      if (evt.kind === "all_done") {
        this.extracting = false;
        return;
      }
      const row = this.progress.find((p) => p.code === evt.course_code);
      if (!row) return;
      if (evt.kind === "course_start") {
        row.status = "working";
        row.statusLabel = "scansione…";
      } else if (evt.kind === "course_progress") {
        row.statusLabel = "estrazione";
        row.message = evt.phase || "";
      } else if (evt.kind === "course_done") {
        row.status = "done";
        row.statusLabel = `${evt.modules} moduli · ${evt.questions} Q&A`;
        row.message = `PDF: ${evt.pdf}`;
      } else if (evt.kind === "course_error") {
        row.status = "error";
        row.statusLabel = "errore";
        row.message = evt.message;
      }
    },

    async openOutput() {
      await window.pywebview.api.openOutputFolder();
    },
  };
}

window.notifyProgress = function (evtJson) {
  try {
    const root = document.querySelector("[x-data]");
    const data = Alpine.$data(root);
    data.onProgress(JSON.parse(evtJson));
  } catch (e) {
    console.error("notifyProgress failed:", e);
  }
};
