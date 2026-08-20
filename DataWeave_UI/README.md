# DataWeave

DataWeave is a Vite + React chat dashboard with a dark/light theme, sidebar navigation, settings, documents, and markdown-based assistant responses.

This project currently uses static demo data so the UI works end to end without a backend. The code is already structured so you can replace the demo layer with live API data later.

## What This Project Includes

- Chat interface with a sidebar and conversation view
- Recent chat list with rename and delete actions
- Settings page with theme selection and model controls
- Documents page for future document-related features
- Theme support for dark, light, and system-style behavior
- Markdown rendering for assistant responses

## Requirements

Before running the app, install:

- Node.js 18 or newer
- npm, which comes with Node.js

If you do not have Node.js installed, download it from:

- https://nodejs.org

After installing Node.js, open a terminal and verify it:

```bash
node -v
npm -v
```

## Setup Steps

Follow these steps from a fresh machine or a fresh clone.

### 1. Clone Or Open The Project

If you are cloning it from git:

```bash
git clone <your-repo-url>
cd DataWeave_UI
```

If the project is already on your machine, just open the project folder in your terminal.

### 2. Install Dependencies

Install everything listed in `package.json`:

```bash
npm install
```

### 3. Start The Development Server

Run the Vite dev server:

```bash
npm run dev
```

Then open the local URL shown in the terminal, usually:

```bash
http://localhost:5173
```

### 4. Build For Production

When you want a production build:

```bash
npm run build
```

### 5. Preview The Production Build

To preview the built app locally:

```bash
npm run preview
```

### 6. Run Lint Checks

To check code quality:

```bash
npm run lint
```

## Project Flow

1. `src/main.jsx` boots the app, loads fonts, and wraps React in the error boundary.
2. `src/App.jsx` sets up routes and mounts the main layout.
3. `src/components/Layout.jsx` initializes the app and applies the selected theme.
4. `src/store/store.js` holds the app state with Zustand.
5. `src/services/api.js` returns static demo data for now.
6. Components and pages read from the store and render the UI.

## Data Flow

- `initApp()` loads chats, messages, documents, settings, and overview data.
- `sendPrompt()` adds the user message and a demo assistant response.
- `newChat()` creates a new chat and switches to it.
- `renameChat()` updates a chat title in the sidebar.
- `deleteChat()` removes a chat and keeps the UI in a valid state.
- `updateSettings()` saves theme and model settings locally.

## Where Live API Data Will Connect Later

The app is static for now, but these functions are already marked as the API hook points:

- `getOverview()` in `src/services/api.js`
- `getChats()` in `src/services/api.js`
- `getMessages(chatId)` in `src/services/api.js`
- `sendMessage(chatId, message)` in `src/services/api.js`
- `createChat(title)` in `src/services/api.js`
- `getDocuments()` in `src/services/api.js`
- `saveSettings(payload)` in `src/services/api.js`
- `getSettings()` in `src/services/api.js`

## File Guide

### Root Files

- `index.html` - Vite HTML entry point and page metadata
- `package.json` - Scripts and dependencies
- `vite.config.js` - Vite configuration
- `README.md` - Project setup and file guide

### `src/main.jsx`

- React entry point
- Loads global fonts
- Mounts the app inside the error boundary

### `src/App.jsx`

- Sets up routing
- Adds the toast provider
- Imports global styles

### `src/components/`

- `Layout.jsx` - Main app shell and theme application
- `Sidebar.jsx` - Navigation, recent chats, and status indicators
- `Header.jsx` - Top bar controls and page header
- `Chat.jsx` - Chat screen and message composer
- `Message.jsx` - Renders user and assistant messages
- `InputBox.jsx` - Chat input form and send behavior
- `Button.jsx` - Shared button wrapper
- `Card.jsx` - Shared card component
- `Loader.jsx` - Loading indicator
- `ErrorBoundary.jsx` - Fallback UI for runtime errors

### `src/pages/`

- `Home.jsx` - Default chat page
- `Documents.jsx` - Documents page
- `Settings.jsx` - Theme and model settings page
- `About.jsx` - Project information page

### `src/services/`

- `api.js` - Static demo data and future API connection layer

### `src/store/`

- `store.js` - Zustand store, actions, and local persistence

### `src/styles/`

- `globals.css` - Theme tokens, layout, spacing, scrollbar, and shared UI styling
- `markdown.css` - Markdown styling for assistant responses

## Installed Packages

The UI uses these packages:

- `tailwindcss`
- `@tailwindcss/vite`
- `react-router-dom`
- `axios`
- `lucide-react`
- `zustand`
- `framer-motion`
- `react-markdown`
- `remark-gfm`
- `rehype-highlight`
- `highlight.js`
- `@fontsource/newsreader`
- `@fontsource/hanken-grotesk`
- `@fontsource/jetbrains-mono`
- `clsx`
- `sonner`
- `react-textarea-autosize`
- `dayjs`

## Notes

- All source files are `.jsx`, not `.tsx`.
- The app currently runs from static demo data.
- The live API integration can be added later without changing the overall UI structure much.
-API integration steps are mention in `api.md`.
- Theme behavior is driven from the settings page and persisted locally.


