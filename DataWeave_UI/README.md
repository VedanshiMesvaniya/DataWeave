# DataWeave UI

This is the React front end for DataWeave — a Vite-powered single-page app that gives you a chat workspace, a sidebar for managing conversations, a document view, and a settings panel, all wired to the FastAPI backend in the parent repository.

The interface talks to the backend through `src/services/api.js`. When the backend isn't running, the same file can fall back to local mock data, which makes it possible to work on layout and UI behavior without spinning up the Python side.

## What's inside

- A chat screen with streaming responses and Markdown rendering
- A sidebar listing recent conversations, with rename and delete support
- A settings screen for switching themes and choosing an LLM provider
- A documents screen for browsing ingested files
- Two selectable themes — **Midnight** (dark) and **Day Light** (light)

## Before you start

You'll need:
- **Node.js 18 or later**
- **npm**, which ships with Node.js

Check what you have installed:
```bash
node -v
npm -v
```
If Node isn't installed, grab it from [nodejs.org](https://nodejs.org).

## Getting it running

### Step 1 — Get the code
If you're starting from a fresh clone:
```bash
git clone <this-repository-url>
cd DataWeave/DataWeave_UI
```
If you already have the repository locally, just `cd` into `DataWeave_UI`.

### Step 2 — Install dependencies
```bash
npm install
```

### Step 3 — Run the dev server
```bash
npm run dev
```
Vite will print a local URL in the terminal — typically:
```
http://localhost:5173
```
Open that in your browser. Changes to source files hot-reload automatically.

### Step 4 — Point it at the backend (optional)
By default the UI expects the FastAPI backend from the root of this repository to be running and reachable. See the root `README.md` for backend setup instructions. If you only need the UI in isolation, the mock data path in `src/services/api.js` lets you browse the interface without a backend running.

### Step 5 — Build for production
```bash
npm run build
```
This produces a static bundle that the FastAPI backend serves directly — no separate frontend server needed in production.

### Step 6 — Preview a production build locally
```bash
npm run preview
```

### Step 7 — Lint the code
```bash
npm run lint
```

## How the app boots

1. `src/main.jsx` mounts the app, loads fonts, and wraps everything in an error boundary.
2. `src/App.jsx` sets up routing and applies the active theme.
3. `src/store/store.js` (Zustand) holds application state — chats, messages, documents, and settings.
4. `src/services/api.js` handles all communication with the backend.
5. Pages and components read from the store and render accordingly.

## State actions worth knowing

- `initApp()` — loads chats, messages, documents, settings, and overview data on startup
- `sendPrompt()` — pushes a user message and streams back the assistant's reply
- `newChat()` — creates a chat and switches the active view to it
- `renameChat()` — updates a chat's title
- `deleteChat()` — removes a chat and keeps the UI in a consistent state afterward
- `updateSettings()` — persists theme and provider preferences

## Backend integration points

These functions in `src/services/api.js` are where the UI talks to the FastAPI backend:

- `getOverview()`
- `getChats()`
- `getMessages(chatId)`
- `sendMessage(chatId, message)`
- `createChat(title)`
- `getDocuments()`
- `saveSettings(payload)`
- `getSettings()`

See `api.md` in this folder for the full request/response contract.

## Folder guide

### Root
- `index.html` — Vite entry HTML
- `package.json` — scripts and dependencies
- `vite.config.js` — build configuration
- `api.md` — backend API contract used by this UI

### `src/components/`
- `Layout.jsx` — app shell, applies the active theme
- `Sidebar.jsx` — navigation, recent chats, upload entry point
- `Header.jsx` — top bar
- `Chat.jsx` — chat screen and message stream
- `Message.jsx` — renders a single message
- `InputBox.jsx` — the message composer
- `MermaidDiagram.jsx` — inline diagram rendering
- `ThinkingTrace.jsx` — collapsible reasoning trace
- `IngestionCard.jsx` — upload progress display
- `Button.jsx`, `Card.jsx`, `Loader.jsx` — shared UI primitives
- `ErrorBoundary.jsx` — catches rendering errors

### `src/pages/`
- `Home.jsx` — the chat page
- `Documents.jsx` — ingested document listing
- `Settings.jsx` — theme and provider selection
- `About.jsx` — project info page

### `src/services/`
- `api.js` — request layer talking to the FastAPI backend
- `http.js` — shared HTTP client setup

### `src/store/`
- `store.js` — global state, actions, and local persistence

### `src/styles/`
- `globals.css` — design tokens, layout, and shared component styling
- `markdown.css` — styling for rendered Markdown in chat responses

### `src/utils/`
- `theme.js` — resolves the active theme (Midnight / Day Light)
- `pdfExport.js` — exports a conversation via the browser's print engine

## Key dependencies

- `react` / `react-dom` — UI runtime
- `react-router-dom` — client-side routing
- `zustand` — state management
- `axios` — HTTP requests to the backend
- `framer-motion` — animation
- `react-markdown`, `remark-gfm`, `rehype-highlight`, `highlight.js` — Markdown rendering with syntax highlighting
- `mermaid` — diagram rendering
- `sonner` — toast notifications
- `tailwindcss` / `@tailwindcss/vite` — styling
- `lucide-react` — icon set
- `dayjs` — date formatting
- `react-textarea-autosize` — auto-growing chat input
- `clsx` — conditional class names
- `jspdf` — PDF export support

## A few things worth knowing

- All source files are `.jsx`, not `.tsx` — this project doesn't use TypeScript.
- Theme selection is driven from the Settings page and persisted locally, with only Midnight and Day Light available.
- The API contract this UI expects from the backend is documented in `api.md`.
