/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                bg: '#081826',
                panel: 'rgba(15, 36, 52, 0.86)',
                panelBorder: 'rgba(94, 136, 170, 0.35)',
                ink: '#d5f3ff',
                muted: '#8fb6cb',
                accent: '#41d6b3',
                warn: '#ff7f50',
                good: '#4ddf7e',
                bad: '#ff6f59',
            },
            fontFamily: {
                mono: ['"IBM Plex Mono"', 'monospace'],
                sans: ['"Space Grotesk"', 'sans-serif'],
            }
        },
    },
    plugins: [],
}
