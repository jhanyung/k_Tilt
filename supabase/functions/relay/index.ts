// Seoul-region fetcher for outlets that refuse connections from GitHub's US runners.
// Not an open proxy: callers need RELAY_KEY, only GET is relayed, and only hosts on
// the outlet allowlist (generated from pipeline/outlets.yaml at deploy time).
// robots.txt is still checked by the pipeline, which fetches it through this relay too.

import allowed from "./allowed.json" with { type: "json" };

const USER_AGENT =
  "GiulgiBot/0.1 (+https://github.com/jhanyung/k_Tilt; news community, links out only)";
const MAX_BYTES = 6_000_000;
const ALLOWED: string[] = allowed.domains;

function hostAllowed(host: string): boolean {
  const h = host.toLowerCase();
  return ALLOWED.some((d) => h === d || h.endsWith("." + d));
}

async function readCapped(res: Response): Promise<Uint8Array | null> {
  if (!res.body) return new Uint8Array();
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

Deno.serve(async (req) => {
  const key = Deno.env.get("RELAY_KEY");
  if (!key || req.headers.get("x-relay-key") !== key) {
    return new Response("forbidden", { status: 403 });
  }
  if (req.method !== "GET") return new Response("method not allowed", { status: 405 });

  const target = new URL(req.url).searchParams.get("url") ?? "";
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return new Response("bad url", { status: 400 });
  }
  if (!["http:", "https:"].includes(url.protocol) || !hostAllowed(url.hostname)) {
    return new Response("host not allowed", { status: 403 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      headers: { "User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*" },
      redirect: "follow",
      signal: AbortSignal.timeout(20_000),
    });
  } catch (e) {
    return new Response(`upstream error: ${(e as Error).name}`, {
      status: 502,
      headers: { "x-relay-error": "upstream" },
    });
  }
  // Redirects must stay on allowed hosts too.
  if (!hostAllowed(new URL(upstream.url).hostname)) {
    return new Response("redirected off allowlist", { status: 403 });
  }
  const body = await readCapped(upstream);
  if (body === null) return new Response("too large", { status: 413 });

  return new Response(body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/octet-stream",
      "x-final-url": upstream.url,
      "x-upstream-status": String(upstream.status),
    },
  });
});
