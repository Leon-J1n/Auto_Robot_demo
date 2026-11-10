import { defineConfig, type ProxyOptions } from "vite";
import { fileURLToPath, URL } from "node:url";

const gatewayTarget = process.env.WEB_GATEWAY_URL ?? "http://127.0.0.1:8099";

/** §5.9 caller routes live at the root, so the dev server has to proxy each of them. */
const callerRoutes = [
  "/getWorld", "/updateWorld", "/updateItem", "/addObject", "/removeObject",
  "/updateTraj", "/captureScene", "/get2DAABB", "/sessions",
];

function isExpectedWebSocketDisconnect(error: unknown): boolean {
  const code = typeof error === "object" && error !== null && "code" in error
    ? String((error as { code?: unknown }).code)
    : "";
  return code === "ECONNABORTED" || code === "ECONNRESET" || code === "EPIPE";
}

const eventsProxy = {
  target: gatewayTarget.replace(/^http/, "ws"),
  ws: true,
  configure(proxy: Parameters<NonNullable<ProxyOptions["configure"]>>[0]): void {
    proxy.on("error", (error: Error & { code?: string }) => {
      if (isExpectedWebSocketDisconnect(error)) return;
      console.error("[vite] /events proxy error", error);
    });
  },
};

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  build: {
    outDir: fileURLToPath(new URL("../dist", import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 650,
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.WEB_FRONTEND_PORT ?? 5173),
    strictPort: false,
    proxy: {
      "/api": gatewayTarget,
      "/events": eventsProxy,
      ...Object.fromEntries(callerRoutes.map((route) => [route, gatewayTarget])),
    },
  },
});
