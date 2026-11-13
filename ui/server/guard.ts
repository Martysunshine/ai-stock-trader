/**
 * Localhost-only access (docs/observability.md: "Bind to 127.0.0.1 only. No authentication exists").
 *
 * Binding to 127.0.0.1 keeps other machines out. The Host check stops DNS rebinding (a web page
 * on another origin resolving its own name to 127.0.0.1), and the Origin check stops other sites
 * from driving the console's own replay controls from a visitor's browser.
 */
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);

export function assertLoopbackBind(host: string): void {
  if (host !== '127.0.0.1' && host !== '::1' && host !== 'localhost') {
    throw new Error(`refusing to bind to ${host}: the console is localhost-only`);
  }
}

function hostnameOf(hostHeader: string | undefined): string | null {
  if (!hostHeader) return null;
  const h = hostHeader.trim().toLowerCase();
  if (h.startsWith('[')) return h.slice(0, h.indexOf(']') + 1);
  return h.split(':')[0];
}

export function isAllowedHost(hostHeader: string | undefined): boolean {
  const name = hostnameOf(hostHeader);
  return name !== null && LOOPBACK_HOSTS.has(name);
}

export function isAllowedOrigin(origin: string | undefined): boolean {
  if (!origin) return true; // same-origin navigations and non-browser clients send none
  try {
    const u = new URL(origin);
    return LOOPBACK_HOSTS.has(u.hostname === '::1' ? '[::1]' : u.hostname);
  } catch {
    return false;
  }
}
