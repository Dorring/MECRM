import type { Metadata } from 'next';
import '../styles/globals.css';
import { Providers } from './providers';
import { AppShell } from './AppShell';

export const metadata: Metadata = {
  title: 'Enterprise CRM',
  description: 'AI-native, multi-tenant Enterprise CRM',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="font-sans" suppressHydrationWarning>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
