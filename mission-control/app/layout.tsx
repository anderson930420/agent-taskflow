import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Taskflow Mission Control",
  description: "Read-only Mission Control dashboard for Agent Taskflow tasks."
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
