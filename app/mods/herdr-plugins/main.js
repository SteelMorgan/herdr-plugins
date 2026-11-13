// Herdr plugins — the App-side control panel for the herdr-plugins instance.
//
// Replaces the TUI plugin runtime surfaces:
//   [[actions]]        -> buttons here + `<mod>.<command>` keys (nextCommand)
//   [[panes]]          -> kept core terminals adopted via herdr.space/tab labels
//   [[startup]]        -> one bounded run of restore-dashboards.py + guard start
//   plugin log list    -> the last action result shown at the bottom
//
// All plugin logic stays in the Python scripts under <root>/plugins; this mod
// only routes. The installer writes <appHome>/herdr-plugins.json with the
// install root, interpreter and default machine.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import {
  exec, hc, run, theme, notify, appHome, home, readTextFile,
  terminals, focused, nextCommand,
} from "herdr";

const PLUGINS = [
  {
    id: "herdr.quota", dir: "herdr-quota", section: "Quota",
    dashboard: { script: "bin/dashboard.py", name: "herdr-quota", title: "LLM Usage" },
    actions: [
      { key: "open", title: "Open dashboard", kind: "dashboard" },
      { key: "diagnose", title: "Diagnose", kind: "script", script: "bin/dashboard.py", args: ["--diagnose"] },
      { key: "setup", title: "DeepSeek setup", kind: "pane", plugin: "herdr.quota.setup", script: "bin/configure_deepseek.py", name: "herdr-quota-setup" },
    ],
  },
  {
    id: "herdr.resources", dir: "herdr-resources", section: "Resources",
    dashboard: { script: "bin/dashboard.py", name: "herdr-resources", title: "Container resources" },
    actions: [
      { key: "open", title: "Open dashboard", kind: "dashboard" },
      { key: "diagnose", title: "Diagnose", kind: "script", script: "bin/dashboard.py", args: ["--diagnose"] },
    ],
  },
  {
    id: "herdr.pve-health", dir: "herdr-pve-health", section: "Server health",
    dashboard: { script: "bin/dashboard.py", name: "herdr-pve-health", title: "Dev server health" },
    actions: [
      { key: "open", title: "Open dashboard", kind: "dashboard" },
      { key: "diagnose", title: "Collect once", kind: "script", script: "bin/dashboard.py", args: ["--diagnose"] },
    ],
  },
  {
    id: "herdr.mimo", dir: "herdr-mimo", section: "MiMo",
    actions: [
      { key: "open", title: "Open MiMo", kind: "mimo", args: ["open-tui"] },
      { key: "start-here", title: "Start in focused terminal", kind: "mimo", args: ["start-here"] },
      { key: "models", title: "Models", kind: "mimo", args: ["models"] },
      { key: "stats", title: "Usage stats", kind: "mimo", args: ["stats"] },
      { key: "llm-server-issue", title: "Mint llm-server token", kind: "mimo", args: ["llm-server-issue"] },
      { key: "llm-server-list", title: "List llm-server tokens", kind: "mimo", args: ["llm-server-list"] },
      { key: "diagnose", title: "Diagnose", kind: "mimo", args: ["diagnose"] },
    ],
  },
  {
    id: "herdr.codex-capacity-guard", dir: "herdr-capacity-guard", section: "Agent guard",
    actions: [
      { key: "start", title: "Start", kind: "guard", args: ["start"] },
      { key: "stop", title: "Stop", kind: "guard", args: ["stop"] },
      { key: "status", title: "Status", kind: "guard", args: ["status"] },
    ],
  },
];

export default class HerdrPlugins extends View {
  init(_props, cx) {
    this.registry = null;
    this.registryError = null;
    this.lastResult = null;
    this.guardStatus = "";
    this.attention = {};
    this.attentionSeen = 0;
    this.busy = {};

    cx.spawn(async (cx) => {
      await this.loadRegistry(cx);
      cx.notify();
      if (this.registry && !globalThis.__herdrPluginsBoot) {
        globalThis.__herdrPluginsBoot = true;
        this.startup(cx);
      }
    });

    // Key commands: "ctrl-shift-u": "herdr-plugins:quota.open" etc.
    cx.spawn(async (cx) => {
      for (;;) {
        const command = await nextCommand();
        await this.handleCommand(command, cx);
        cx.notify();
      }
    });

    // Guard attention (labels + toasts) and status; gentle polling of two
    // small files, no terminal reads.
    cx.timer.every(5000, (cx) => {
      cx.spawn(async (cx) => {
        await this.refreshAttention(cx);
        cx.notify();
      });
    });
    cx.timer.every(15000, (cx) => {
      cx.spawn(async (cx) => {
        await this.refreshGuardStatus(cx);
        cx.notify();
      });
    });
  }

  async loadRegistry(cx) {
    try {
      const text = await readTextFile(appHome() + "/herdr-plugins.json");
      if (!text) {
        this.registryError = "Not installed: run install.py from the herdr-plugins repo.";
        return;
      }
      this.registry = JSON.parse(text);
    } catch (e) {
      this.registryError = "Registry error: " + e;
    }
  }

  root() { return this.registry?.root ?? ""; }
  python() { return this.registry?.python ?? "python3"; }
  machine() { return this.registry?.machine ?? null; }
  space() { return this.registry?.space ?? "dashboards"; }

  // Where plugin scripts run: the default machine (registry.machines) or
  // this computer. The guard always runs centrally and watches every machine.
  target() {
    const machine = this.machine();
    const remote = machine ? this.registry?.machines?.[machine] : null;
    if (machine && remote) {
      return { root: remote.root, python: remote.python ?? "python3", on: machine };
    }
    return { root: this.root(), python: this.python(), on: null };
  }

  pluginScript(plugin, script, target = this.target()) {
    return target.root + "/plugins/" + plugin.dir + "/" + script;
  }

  async startup(cx) {
    // TUI [[startup]] hooks: bounded dashboard restoration + guard start.
    // Both scripts are locked and idempotent; focus is never touched.
    const restorer = this.root() + "/plugins/herdr-resources/bin/restore-dashboards.py";
    const guard = this.root() + "/plugins/herdr-capacity-guard/bin/guard.py";
    const restorerArgs = [restorer, "--space", this.space()];
    if (this.machine()) restorerArgs.push("--machine", this.machine());
    try {
      await exec(this.python(), restorerArgs, { timeout: 120000 });
    } catch (e) { /* the restorer logs its own retries */ }
    try {
      await exec(this.python(), [guard, "start"], { timeout: 30000 });
    } catch (e) { /* guard start is best-effort at boot */ }
    await this.refreshGuardStatus(cx);
  }

  guardStateDir() {
    return home() + "/.local/state/herdr-app/herdr-agent-guard";
  }

  async refreshAttention(cx) {
    try {
      const text = await readTextFile(this.guardStateDir() + "/attention.json");
      const entries = text ? JSON.parse(text) : {};
      const now = Date.now() / 1000;
      for (const [address, entry] of Object.entries(entries)) {
        if (entry.expires_at && entry.expires_at < now) continue;
        if (entry.notify && entry.created_at > this.attentionSeen) {
          notify({
            title: (entry.agent === "dsh-tui" ? "DSH TUI" : "Codex") + " требует внимания",
            body: address + ": " + entry.reason,
            level: "warning",
            action: { name: "agent.focus", args: { terminal: address } },
          });
        }
      }
      this.attentionSeen = now;
      this.attention = entries;
    } catch (e) {
      this.attention = {};
    }
  }

  async refreshGuardStatus(cx) {
    if (!this.registry) return;
    try {
      const guard = this.root() + "/plugins/herdr-capacity-guard/bin/guard.py";
      const r = await exec(this.python(), [guard, "status"], { timeout: 20000 });
      this.guardStatus = (r.stdout || r.stderr || "").trim();
    } catch (e) {
      this.guardStatus = "status unavailable";
    }
  }

  async handleCommand(command, cx) {
    // "<plugin>.<action>" — e.g. quota.open, guard.start, mimo.open.
    const aliases = { quota: 0, resources: 1, pve: 2, "pve-health": 2, mimo: 3, guard: 4, "capacity-guard": 4 };
    const [alias, actionKey] = String(command).split(".");
    const index = aliases[alias];
    if (index === undefined) return;
    const plugin = PLUGINS[index];
    const action = plugin.actions.find((a) => a.key === actionKey);
    if (action) await this.runAction(plugin, action, cx);
  }

  async runAction(plugin, action, cx) {
    const busyKey = plugin.id + "." + action.key;
    if (this.busy[busyKey]) return;
    this.busy[busyKey] = true;
    cx.notify();
    const target = this.target();
    const execOpts = { timeout: 120000 };
    if (target.on) execOpts.on = target.on;
    try {
      let result;
      if (action.kind === "dashboard") {
        result = await this.openPane(plugin.id, this.pluginScript(plugin, plugin.dashboard.script, target),
          plugin.dashboard.name, target.root + "/plugins/" + plugin.dir, target);
      } else if (action.kind === "pane") {
        result = await this.openPane(action.plugin, this.pluginScript(plugin, action.script, target),
          action.name, target.root + "/plugins/" + plugin.dir, target);
      } else if (action.kind === "mimo") {
        const args = [this.pluginScript(plugin, "bin/mimo.py", target), ...action.args];
        const terminal = focused()?.terminal;
        if (terminal) args.push("--terminal", terminal);
        result = await exec(target.python, args, { ...execOpts, timeout: 60000 });
      } else if (action.kind === "guard") {
        // The guard is central: it watches local and managed machines alike.
        const guard = this.root() + "/plugins/herdr-capacity-guard/bin/guard.py";
        result = await exec(this.python(), [guard, ...action.args], { timeout: 30000 });
        await this.refreshGuardStatus(cx);
      } else {
        result = await exec(target.python, [this.pluginScript(plugin, action.script, target), ...(action.args ?? [])], execOpts);
      }
      const text = ((result?.stdout ?? "") + (result?.stderr ?? "")).trim();
      this.lastResult = {
        title: plugin.section + " · " + action.title,
        text: text.slice(0, 2000) || "done",
        error: (result?.code ?? 0) !== 0,
      };
    } catch (e) {
      this.lastResult = { title: plugin.section + " · " + action.title, text: String(e), error: true };
    } finally {
      this.busy[busyKey] = false;
      cx.notify();
    }
  }

  async openPane(pluginId, script, name, cwd, target) {
    // open_pane.py runs locally and drives hc/herdr-app; the terminal itself
    // starts on the target machine with that machine's interpreter and paths.
    const argv = JSON.stringify([target.python, script]);
    const args = [
      this.root() + "/lib/open_pane.py", pluginId,
      "--argv", argv, "--name", name, "--cwd", cwd, "--space", this.space(),
    ];
    if (target.on) args.push("--machine", target.on);
    const r = await exec(this.python(), args, { timeout: 60000 });
    let payload = {};
    try { payload = JSON.parse((r.stdout || "").trim()); } catch (e) {}
    if (payload.error) throw new Error(payload.error);
    return { code: r.code, stdout: "terminal " + (payload.terminal ?? "?"), stderr: r.stderr };
  }

  render(cx) {
    const t = theme();
    if (!this.registry) {
      return v_flex().size_full().bg(t.surface).p(12).gap(8).text_size(13)
        .child(div().text_color(t.foreground).font_semibold().child("Herdr plugins"))
        .child(div().text_color(t.muted).child(this.registryError ?? "Loading…"));
    }
    const guardLabels = terminals()
      .filter((term) => term.labels && term.labels["herdr.guard"])
      .map((term) => term.address + ": " + term.labels["herdr.guard"]);

    const sections = PLUGINS.map((plugin) => {
      const buttons = plugin.actions.map((action) => {
        const busyKey = plugin.id + "." + action.key;
        const label = this.busy[busyKey] ? "…" : action.title;
        return div()
          .id(plugin.id + "-" + action.key)
          .px(8).py(4).rounded(6).bg(t.raised)
          .text_size(12).text_color(t.foreground)
          .hover((s) => s.bg(t.hover))
          .on_click((_e, cx) => { cx.spawn(async (cx) => { await this.runAction(plugin, action, cx); cx.notify(); }); })
          .child(label);
      });
      return v_flex().gap(4).child(
        div().text_size(12).font_semibold().text_color(t.accent).child(plugin.section),
      ).child(h_flex().gap(4).flex_wrap().children(buttons));
    });

    const result = this.lastResult
      ? v_flex().gap(2).mt(6).p(8).rounded(6).bg(t.raised).children([
          div().text_size(11).font_semibold()
            .text_color(this.lastResult.error ? t.blocked : t.done)
            .child(this.lastResult.title),
          div().text_size(11).text_color(t.muted).child(this.lastResult.text),
        ])
      : div();

    return v_flex().size_full().bg(t.surface).p(10).gap(8).text_size(13)
      .overflow_y_scrollbar()
      .child(div().font_semibold().text_color(t.foreground).child("Herdr plugins"))
      .child(div().text_size(11).text_color(t.dim)
        .child("guard: " + (this.guardStatus || "…")))
      .when(guardLabels.length > 0, (el) => el.children(
        guardLabels.map((line) => div().text_size(11).text_color(t.blocked).child(line)),
      ))
      .children(sections)
      .child(result);
  }
}
