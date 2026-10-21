// The site's Worker: serves the static pages, and on a timer starts the GitHub jobs at
// the right times (GitHub's own scheduler runs them hours late under load).
// Needs one secret, GITHUB_TOKEN: a fine-grained token for jhanyung/k_Tilt with
// "Actions: Read and write". Without it the timer does nothing.

const REPO = "jhanyung/k_Tilt";

// [workflow file, test(minute, hour) in UTC]
const JOBS = [
  ["ingest.yml", (m, h) => m % 30 === 10],                 // every 30 minutes
  ["edition.yml", (m, h) => (h === 22 || h === 9) && m === 0], // 07:00 and 18:00 KST
  ["lexicon.yml", (m, h) => h === 18 && m === 50],          // 03:50 KST
  ["discover.yml", (m, h, d) => d === 0 && h === 18 && m === 20], // Monday 03:20 KST
];

async function dispatch(env, workflow) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "giulgi-scheduler",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  return `${workflow}: ${res.status}`;
}

export default {
  async fetch(request, env) {
    return env.ASSETS.fetch(request);
  },

  async scheduled(event, env, ctx) {
    if (!env.GITHUB_TOKEN) return;
    const t = new Date(event.scheduledTime);
    const m = t.getUTCMinutes(), h = t.getUTCHours(), d = t.getUTCDay();
    const due = JOBS.filter(([, test]) => test(m, h, d)).map(([w]) => w);
    ctx.waitUntil(Promise.all(due.map((w) => dispatch(env, w))).then((r) => console.log(r.join(", "))));
  },
};
