import type { NextConfig } from "next";

// The dashboard is a client-rendered app talking to the local API, so the
// server-caching features of the default template are not enabled.
const nextConfig: NextConfig = {
  // The browser test runs its own dev server next to a normal one, so it needs its own build directory.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
