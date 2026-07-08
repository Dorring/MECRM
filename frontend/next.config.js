/** @type {import('next').NextConfig} */
const path = require('path');

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  turbopack: {},
  webpack: (config) => {
    config.resolve.alias = {
      ...(config.resolve.alias || {}),
      '@': path.resolve(__dirname, 'src'),
    };
    return config;
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        // GATEWAY_INTERNAL_URL is a server-side build/start-time variable,
        // NOT a browser NEXT_PUBLIC_* variable. It is inlined into the
        // Next.js server bundle, never exposed to the browser.
        destination: `${process.env.GATEWAY_INTERNAL_URL || 'http://localhost:4000'}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
