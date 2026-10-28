// Demo backend: believable fake data in the same shape as the real API.
// Open the UI with ?demo to use it (handy for screenshots and UI work
// without a server).

const GiB = 1024 ** 3;
const now = () => Date.now() / 1000;
const BOOT = now() - (2 * 86400 + 7 * 3600 + 12 * 60);

let seed = 7;
function rnd() { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; }
function walk(v, target, spread, lo, hi) {
  v += (target - v) * 0.15 + (rnd() - 0.5) * spread;
  return Math.min(hi, Math.max(lo, v));
}

const GROUPS = [
  { id: 'infrastructure', name: 'Infrastructure' },
  { id: 'media', name: 'Media' },
  { id: 'personal', name: 'Personal apps' },
  { id: 'monitoring', name: 'Monitoring' },
];
const PLAN = [
  ['stx-monitor', 'stx-monitor', 'monitoring', 'This console', 8090, ['docker-proxy']],
  ['docker-proxy', 'Socket proxy', 'monitoring', 'Read-only gate to the Docker API', null, []],
  ['ntfy', 'ntfy', 'monitoring', 'Push alerts to my iPhone', 2586, []],
  ['caddy', 'Caddy', 'infrastructure', 'HTTPS and subdomains for every app', 443, []],
  ['backups', 'Backups', 'infrastructure', 'Nightly restic backups of app configs', null, []],
  ['jellyfin', 'Jellyfin', 'media', 'Streams movies and shows, Quick Sync transcoding', 8096, []],
  ['jellyseerr', 'Jellyseerr', 'media', 'Request movies and shows', 5055, ['sonarr', 'radarr', 'jellyfin']],
  ['sonarr', 'Sonarr', 'media', 'Finds and organizes TV shows', 8989, ['prowlarr', 'qbittorrent']],
  ['radarr', 'Radarr', 'media', 'Finds and organizes movies', 7878, ['prowlarr', 'qbittorrent']],
  ['prowlarr', 'Prowlarr', 'media', 'Manages indexers for Sonarr and Radarr', 9696, []],
  ['qbittorrent', 'qBittorrent', 'media', 'Downloads, only through the VPN', 8080, ['gluetun']],
  ['gluetun', 'Gluetun', 'media', 'VPN tunnel for downloads', null, []],
  ['opengym', 'openGym', 'personal', 'Gym tracker for my iPhone', null, []],
].map(([id, name, group, description, port, links]) => ({ id, name, group, description, port, url: null, links }));

const CONTAINERS = {
  'stx-monitor': { id: '3f2a9c1d8e01', name: 'stx-monitor', image: 'stx-monitor:3f2a9c1', cpu: 0.6, mem: 74e6, up: 2 * 3600 + 22 * 60 },
  'docker-proxy': { id: 'b81c0e44f2a7', name: 'stx-docker-proxy', image: 'tecnativa/docker-socket-proxy:v0.5.0', cpu: 0.05, mem: 9e6, up: 2 * 86400 + 7 * 3600 },
  jellyfin: { id: 'c94d1f0a6b22', name: 'jellyfin', image: 'jellyfin/jellyfin:10.11', cpu: 4.1, mem: 612e6, up: 2 * 86400 + 6 * 3600 },
  sonarr: { id: 'd0e7a3b5c911', name: 'sonarr', image: 'lscr.io/linuxserver/sonarr:latest', cpu: 2.0, mem: 180e6, up: 26 * 60 },
  prowlarr: { id: 'e12f9b7c4d30', name: 'prowlarr', image: 'lscr.io/linuxserver/prowlarr:latest', cpu: 1.1, mem: 140e6, up: 2 * 86400 + 6 * 3600 },
  radarr: { id: 'f5a8c2e19b47', name: 'radarr', image: 'lscr.io/linuxserver/radarr:latest', down: true, stoppedAgo: 12 * 60, ranFor: 27 * 3600 },
};

const state = {
  cpu: 9, mem: 37.4, temp: 52, watts: 11, rx: 4.8e6, tx: 0.26e6, fan: 2100,
  cores: Array.from({ length: 12 }, () => 6 + rnd() * 8),
};

function containerList() {
  return Object.values(CONTAINERS).map((c) => ({
    id: c.id, full_id: c.id.padEnd(64, '0'), name: c.name, image: c.image,
    state: c.down ? 'exited' : 'running',
    status: c.down ? `Exited (137) ${Math.round((c.stoppedAgo + (now() - T0)) / 60)} minutes ago` : `Up ${Math.round(c.up / 3600) || 1} hours`,
    health: null, created: Math.round(now() - 3 * 86400), ports: [], labels: {},
    compose_project: 'stx', compose_service: c.name,
    stats: c.down ? null : { cpu: +(c.cpu * (0.8 + rnd() * 0.4)).toFixed(2), mem: Math.round(c.mem * (0.97 + rnd() * 0.06)), mem_limit: c.name === 'radarr' ? 2 * GiB : 16.6e9 },
  }));
}

function appsMap(containers) {
  const byName = new Map(containers.map((c) => [c.name, c]));
  const groups = GROUPS.map((g) => ({ ...g, apps: [] }));
  const links = [];
  for (const app of PLAN) {
    const spec = CONTAINERS[app.id];
    const c = spec ? byName.get(spec.name) : null;
    const status = !c ? 'planned' : c.state === 'running' ? 'running' : 'down';
    groups.find((g) => g.id === app.group).apps.push({
      ...app, status,
      container: c ? { id: c.id, name: c.name, image: c.image, state: c.state, status: c.status, health: null, ports: [], cpu: c.stats?.cpu ?? null, mem: c.stats?.mem ?? null, mem_limit: c.stats?.mem_limit ?? null } : null,
    });
    for (const t of app.links) links.push([app.id, t]);
  }
  const all = groups.flatMap((g) => g.apps);
  const counts = {
    total: all.length,
    running: all.filter((a) => a.status === 'running').length,
    down: all.filter((a) => a.status === 'down').length,
    planned: all.filter((a) => a.status === 'planned').length,
  };
  counts.deployed = counts.total - counts.planned;
  return { groups, links, counts, error: null };
}

function host() {
  const t = now();
  state.cpu = walk(state.cpu, 9, 3, 2, 60);
  state.mem = walk(state.mem, 37.4, 0.4, 30, 50);
  state.temp = walk(state.temp, 50 + state.cpu * 0.25, 1.2, 38, 95);
  state.watts = walk(state.watts, 8 + state.cpu * 0.35, 1.2, 4, 45);
  state.rx = walk(state.rx, 4.8e6, 0.9e6, 0.1e6, 12e6);
  state.tx = walk(state.tx, 0.26e6, 0.08e6, 0.02e6, 2e6);
  state.fan = Math.round(walk(state.fan, 1900 + state.temp * 4, 60, 0, 5000) / 10) * 10;
  state.cores = state.cores.map((v) => walk(v, state.cpu, 6, 0, 100));
  const memTotal = 16642998272;
  const memUsed = memTotal * state.mem / 100;
  const coreTemps = [0, 1, 2, 3, 4, 5].map((i) => ({ chip: 'coretemp', label: `Core ${i}`, c: Math.round(state.temp - 2 + rnd() * 4), crit: 100 }));
  const proc = (pid, name, user, cpu, rss, container = null) => ({ pid, name, user, cpu: +cpu.toFixed(1), rss, container });
  const procs = [
    proc(2211, 'jellyfin', 'isaac', 4.1 + rnd(), 612e6, CONTAINERS.jellyfin.id),
    proc(3150, 'Sonarr', 'isaac', 2.0 + rnd() * 0.5, 180e6, CONTAINERS.sonarr.id),
    proc(3402, 'Prowlarr', 'isaac', 1.1 + rnd() * 0.3, 140e6, CONTAINERS.prowlarr.id),
    proc(1104, 'dockerd', 'root', 0.7 + rnd() * 0.2, 96e6),
    proc(4120, 'python', 'root', 0.6 + rnd() * 0.2, 74e6, CONTAINERS['stx-monitor'].id),
    proc(988, 'tailscaled', 'root', 0.3 + rnd() * 0.1, 42e6),
    proc(1020, 'containerd', 'root', 0.2, 48e6),
    proc(412, 'systemd-journal', 'root', 0.1, 31e6),
  ];
  return {
    time: t,
    uptime_s: t - BOOT,
    cpu: {
      percent: +state.cpu.toFixed(1), per_core: state.cores.map((v) => +v.toFixed(1)), freq_mhz: Math.round(2200 + state.cpu * 15),
      freq_max_mhz: 3900, load: [0.62, 0.55, 0.48], throttle: { core: 0, package: 0 }, throttling: false,
    },
    memory: { total: memTotal, used: memUsed, available: memTotal - memUsed, cached: 4.1 * GiB, percent: +state.mem.toFixed(1), swap_total: 4 * GiB, swap_used: 0, swap_percent: 0 },
    network: [
      { name: 'enp3s0', kind: 'ethernet', physical: true, state: 'up', speed_mbps: 1000, ipv4: '192.168.1.134', rx_rate: Math.round(state.rx), tx_rate: Math.round(state.tx) },
      { name: 'tailscale0', kind: 'tailscale', physical: false, state: 'unknown', speed_mbps: null, ipv4: '100.101.12.4', rx_rate: 1200, tx_rate: 900 },
    ],
    mounts: [
      { mountpoint: '/', device: '/dev/mapper/ubuntu--vg-ubuntu--lv', fstype: 'ext4', readonly: false, block: 'dm-0', total: 113 * GiB, used: 18.2 * GiB, free: 89 * GiB, percent: 16.1 },
    ],
    disks: [
      { name: 'sda', model: 'SanDisk X600', size: 128e9, kind: 'ssd', usb: false, removable: false, partitions: ['sda1', 'sda2', 'sda3'], filesystems: ['ext4', 'LVM2_member', 'vfat'], mounts: ['/'], state: 'mounted' },
      { name: 'sdb', model: 'ST1000LM035-1RK172', size: 1000204886016, kind: 'hdd', usb: false, removable: false, partitions: [], filesystems: [], mounts: [], state: 'not mounted' },
      { name: 'sdc', model: 'Samsung Portable SSD T7', size: 1000204886016, kind: 'ssd', usb: true, removable: false, partitions: [], filesystems: [], mounts: [], state: 'not mounted' },
    ],
    disk_io: { sda: { read_rate: Math.round(0.4e6 * (0.5 + rnd())), write_rate: Math.round(1.2e6 * (0.5 + rnd())) }, sdb: { read_rate: 0, write_rate: 0 }, sdc: { read_rate: 0, write_rate: 0 } },
    cpu_temp: Math.round(state.temp),
    temps: [
      { chip: 'coretemp', label: 'Package id 0', c: Math.round(state.temp), crit: 100 },
      ...coreTemps,
      { chip: 'dell_smm', label: 'CPU', c: Math.round(state.temp - 4), crit: null },
      { chip: 'dell_smm', label: 'Ambient', c: 36, crit: null },
      { chip: 'dell_smm', label: 'SODIMM', c: 41, crit: null },
      { chip: 'pch_cannonlake', label: 'temp1', c: 47, crit: null },
    ],
    fans: [{ chip: 'dell_smm', label: 'Processor Fan', rpm: state.fan }, { chip: 'dell_smm', label: 'Video Fan', rpm: 0 }],
    power_supply: { ac_online: true, battery: { name: 'BAT0', capacity: 80, status: 'Not charging', health: 91.2, cycle_count: 214, power_w: 0, threshold_start: null, threshold_end: null, model: 'DELL 0GFJ66A' } },
    power: { cpu_package_w: +state.watts.toFixed(1), system_w: null, watts: +state.watts.toFixed(1), source: 'rapl', label: 'CPU package only (RAPL); the whole laptop draws more', rate: 0.15, monthly_kwh: +(state.watts * 0.73).toFixed(2), monthly_cost: +(state.watts * 0.73 * 0.15).toFixed(2) },
    gpus: [
      { slot: '0000:00:02.0', vendor: 'Intel', name: 'UHD Graphics 630 (Mobile)', driver: 'i915', runtime_status: 'active', boot_vga: true, render_node: 'renderD128' },
      { slot: '0000:01:00.0', vendor: 'NVIDIA', name: 'GeForce GTX 1060 Mobile', driver: 'nouveau', runtime_status: 'suspended', boot_vga: false, render_node: 'renderD129' },
    ],
    render_node: true,
    lid: 'closed',
    processes: { count: 214, by_cpu: procs, by_mem: [...procs].sort((a, b) => b.rss - a.rss) },
  };
}

function pointOf(h) {
  return [Math.round(h.time * 10) / 10, h.cpu.percent, h.memory.percent, h.cpu_temp, h.network[0].rx_rate, h.network[0].tx_rate, h.power.watts];
}

function makeHistory() {
  const t = now();
  const live = [];
  for (let i = 1199; i >= 1; i--) {
    const tt = t - i * 3;
    const wave = Math.sin(i / 40) * 3 + (i % 300 < 20 ? 18 : 0);
    live.push([tt, +(9 + wave + rnd() * 3).toFixed(1), +(37 + Math.sin(i / 200)).toFixed(1), Math.round(51 + wave * 0.6 + rnd() * 2), Math.round(4.5e6 + Math.sin(i / 30) * 1.5e6 + rnd() * 0.8e6), Math.round(0.25e6 + rnd() * 0.1e6), +(10 + wave * 0.4 + rnd()).toFixed(1)]);
  }
  const day = [];
  for (let i = 1439; i >= 1; i--) {
    const tt = Math.floor((t - i * 60) / 60) * 60;
    const hour = new Date(tt * 1000).getHours();
    const evening = hour >= 19 && hour <= 23 ? 1 : 0; // movie night: Jellyfin busy
    day.push([tt, +(7 + evening * 14 + rnd() * 4).toFixed(1), +(34 + evening * 6 + rnd()).toFixed(1), Math.round(48 + evening * 8 + rnd() * 3), Math.round(1e6 + evening * 6e6 + rnd() * 1e6), Math.round(0.2e6 + evening * 2.4e6 + rnd() * 0.2e6), +(8 + evening * 6 + rnd()).toFixed(1)]);
  }
  return { fields: ['t', 'cpu', 'mem', 'temp', 'rx', 'tx', 'watts'], live, day };
}

const T0 = now();
const DOCKER = { enabled: true, ok: true, error: null, events: true, info: { version: '29.0.1', containers: 6, running: 5, images: 9, storage_driver: 'overlay2', cgroup_version: '2' } };
const ev = (id, agoMin, kind, level, title, detail, container = null) => ({ id, time: T0 - agoMin * 60, kind, level, title, detail, container });
const EVENTS = [
  ev(1, 142, 'system', 'info', 'stx-monitor started', 'Build 3f2a9c1, version 0.1.0.'),
  ev(2, 58, 'power', 'warn', 'Running on battery', 'Wall power may be out. Battery at 80%.'),
  ev(3, 54, 'power', 'ok', 'Wall power is back', 'The server ran on battery for 4 min.'),
  ev(4, 26, 'app', 'ok', 'sonarr started', 'Healthy again after a restart.', CONTAINERS.sonarr.id),
  ev(5, 12.2, 'app', 'error', 'radarr ran out of memory', 'Linux ended it because it hit its memory limit.', CONTAINERS.radarr.id),
  ev(6, 12, 'app', 'error', 'radarr stopped', 'Exit code 137: it was killed, usually for running out of memory or a forced stop.', CONTAINERS.radarr.id),
];

function logsFor(name, tail) {
  const t = T0;
  const iso = (agoS) => new Date((t - agoS) * 1000).toISOString();
  let lines;
  if (name === 'radarr') {
    lines = [
      [16 * 60, 'Info  Starting RSS sync'], [15.9 * 60, 'Info  Checked 112 releases from 3 indexers'],
      [15 * 60, 'Info  Refreshing movie library'], [14.5 * 60, 'Info  Scanning 1,240 files'],
      [13.4 * 60, 'Warn  Library scan is using 1.6 GB of memory'], [12.6 * 60, 'Warn  Library scan is using 1.9 GB of memory'],
      [12 * 60 + 1, 'Error Out of memory while reading media info'],
    ];
  } else {
    lines = Array.from({ length: 30 }, (_, i) => [(30 - i) * 47, i % 9 === 4 ? 'Warn  Slow response from indexer (2.1 s)' : ['Info  Health check passed', 'Info  Scheduled task finished in 210 ms', 'Info  RSS sync complete, 0 new releases', 'Info  Request served in 12 ms'][i % 4]]);
  }
  return lines.slice(-tail).map(([a, text]) => ({ ts: iso(a), stream: /Error/.test(text) ? 'stderr' : 'stdout', text }));
}

export function createDemo() {
  let current = host();
  const history = makeHistory();
  history.live.push(pointOf(current));
  return {
    async get(path) {
      await new Promise((r) => setTimeout(r, 60));
      if (path.startsWith('api/overview')) {
        const containers = containerList();
        return {
          identity: { hostname: 'stx-server', os: 'Ubuntu 26.04.1 LTS', kernel: '7.0.0-14-generic', vendor: 'Dell Inc.', product: 'G7 7588', cpu_model: 'Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz', cpu_threads: 12, cpu_cores: 6, boot_time: BOOT },
          host: current, containers, apps: appsMap(containers), history, events: EVENTS,
          tailnet: {
            state: 'Running', tailnet: 'isaacxj.github', magic_dns_suffix: 'tail4c1f2.ts.net', online: 2,
            devices: [
              { name: 'stx-server', os: 'linux', ip: '100.101.12.4', online: true, last_seen: null, self: true },
              { name: 'isaacs-laptop', os: 'windows', ip: '100.88.3.17', online: true, last_seen: null, self: false },
              { name: 'iphone', os: 'iOS', ip: '100.92.41.8', online: false, last_seen: new Date((T0 - 2 * 3600) * 1000).toISOString(), self: false },
            ],
          },
          docker: DOCKER,
          build: { sha: '3f2a9c1', version: '0.1.0', started: T0 - 142 * 60 },
          power_rate: 0.15,
          owner: 'Isaac',
        };
      }
      const m = /^api\/containers\/([^/?]+)(\/logs)?(?:\?tail=(\d+))?/.exec(path);
      if (m) {
        const spec = Object.values(CONTAINERS).find((c) => c.id === decodeURIComponent(m[1]));
        if (!spec) throw new Error('404 no such container');
        if (m[2]) return { id: spec.id, name: spec.name, lines: logsFor(spec.name, Number(m[3] || 200)) };
        const c = containerList().find((x) => x.id === spec.id);
        const started = spec.down ? T0 - spec.stoppedAgo - spec.ranFor : T0 - spec.up;
        return {
          ...c,
          inspect: {
            started_at: new Date(started * 1000).toISOString(),
            finished_at: spec.down ? new Date((T0 - spec.stoppedAgo) * 1000).toISOString() : '0001-01-01T00:00:00Z',
            exit_code: spec.down ? 137 : 0, oom_killed: !!spec.down, error: null, health: null,
            restart_count: spec.name === 'radarr' ? 3 : 0, restart_policy: 'unless-stopped',
            memory_limit: spec.name === 'radarr' ? 2 * GiB : null, network_mode: 'bridge', image: spec.image, tty: false, cmd: null, mounts: [],
          },
          app: null, events: [],
        };
      }
      throw new Error(`demo: no data for ${path}`);
    },
    subscribe(handlers) {
      setInterval(() => {
        current = host();
        const containers = containerList();
        handlers.tick({ host: current, containers, apps: appsMap(containers), point: pointOf(current), docker: DOCKER });
      }, 3000);
    },
  };
}
