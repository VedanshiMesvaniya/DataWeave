# Connecting the FastAPI Backend to the React UI

This doc walks through how to wire up a FastAPI backend to the existing React UI using Axios, without breaking anything that already works.

The short version: we're not touching the components. We're swapping out the data layer in `src/services/api.js`, keeping the Zustand store (`src/store/store.js`) as the one bridge between the UI and the backend, and migrating endpoints one at a time so the app stays usable while the backend is still being built.

## How data flows right now

At the moment the app runs entirely on static demo data:

- `src/services/api.js` just returns hardcoded objects.
- `src/store/store.js` calls those functions and drops the result into Zustand.
- Everything downstream — `Chat.jsx`, `Sidebar.jsx`, `Settings.jsx`, `Documents.jsx` — only ever reads from the store.

Since none of the components talk to the API directly, the service layer is the natural (and really the only sensible) place to plug in FastAPI.

## The integration strategy, in order

1. Build the FastAPI backend.
2. Add a basic Axios setup on the frontend.
3. Swap the demo functions in `src/services/api.js` for real HTTP calls.
4. Keep the JSON shapes close to what the demo data already returns.
5. Leave the store and components alone as much as possible.
6. Worry about streaming later, once the basics work.

Doing it in this order means the app keeps working the whole time — you're never in a state where the frontend is broken because the backend isn't finished yet.

## Endpoints to build

Here's what maps to the current app structure:

| Feature | Method | Endpoint | Used by |
|---|---:|---|---|
| App overview | `GET` | `/overview` | `initApp()` |
| Chat list | `GET` | `/chats` | `initApp()`, `newChat()`, sidebar |
| Chat messages | `GET` | `/chats/{chatId}/messages` | `selectChat()`, `initApp()` |
| Send message | `POST` | `/chats/{chatId}/messages` | `sendPrompt()` |
| Create chat | `POST` | `/chats` | `newChat()` |
| Rename chat | `PATCH` | `/chats/{chatId}` | `renameChat()` |
| Delete chat | `DELETE` | `/chats/{chatId}` | `deleteChat()` |
| Documents list | `GET` | `/documents` | `refreshDocuments()`, `initApp()` |
| Settings load | `GET` | `/settings` | `initApp()` |
| Settings save | `POST` / `PUT` | `/settings` | `updateSettings()` |

If you eventually want token-by-token streaming, you'll need one of:

- `GET /chats/{chatId}/stream` (Server-Sent Events)
- A WebSocket-based chat stream

Axios handles normal request/response just fine, but it's not built for streaming — SSE or WebSockets are the better tool for that part.

## Setting up Axios

Keep the base URL and headers in one place rather than scattering `axios.create()` calls everywhere. A dedicated file works well:

**`src/services/http.js`**

This file should:

- Read `VITE_API_BASE_URL` from the environment
- Set the JSON content-type header
- Attach an auth token, if the backend needs one
- Handle errors in a central spot instead of repeating try/catch everywhere

```js
import axios from 'axios'

export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  headers: {
    'Content-Type': 'application/json',
  },
})
```

From there, `src/services/api.js` just calls `http.get()`, `http.post()`, etc.

## Don't touch the UI yet

The trick to not breaking anything is keeping the exact same function names exported from `src/services/api.js`:

- `getOverview()`
- `getChats()`
- `getMessages(chatId)`
- `sendMessage(chatId, message)`
- `createChat(title)`
- `getDocuments()`
- `saveSettings(payload)`
- `getSettings()`

As long as those names and their return shapes don't change, nothing downstream needs to know or care that real data is now coming from FastAPI instead of a mock. Concretely, that means:

- `store.js` needs little to no change.
- `Chat.jsx` keeps calling `sendPrompt()` like before.
- `Sidebar.jsx` keeps using the same chat actions.
- `Settings.jsx` keeps reading settings from the store.

## Which files actually touch API data

| File | Role | Impact of going live |
|---|---|---|
| `src/services/api.js` | Data bridge | Demo data gets replaced with Axios calls |
| `src/store/store.js` | State orchestration | Maps API responses into store state |
| `src/components/Chat.jsx` | Chat UI | No real change needed if shapes stay consistent |
| `src/components/Message.jsx` | Message rendering | No API code involved at all |
| `src/components/Sidebar.jsx` | Chat list & actions | Consumes chat list, rename, delete, new-chat data |
| `src/pages/Settings.jsx` | Model & theme settings | Reads settings from the store |
| `src/components/Layout.jsx` | App init | Calls `initApp()`, applies theme |
| `src/pages/Documents.jsx` | Document list UI | Reads document data from the store |

## What actually needs to change

Focus on just these three:

1. `src/services/api.js`
2. `src/store/store.js`
3. `src/services/http.js` (new file, for the shared Axios client)

Everything else is optional, and only needed if the backend's data shape genuinely differs from the demo:

- `Chat.jsx` — only if you add streaming or server-side typing indicators
- `Message.jsx` — only if the backend sends richer message metadata
- `Settings.jsx` — only if setting keys differ from the demo
- `Documents.jsx` — only if document fields differ

If FastAPI returns data shaped like the demo data, the component layer barely notices the switch.

## Payload shapes to aim for

Matching these shapes keeps the frontend changes minimal.

**Overview**
```json
{
  "backendStatus": "online",
  "ollamaStatus": "active",
  "vectorStatus": "ready",
  "modelLabel": "Mistral 7B Instruct",
  "contextTokens": 4096,
  "privacyLabel": "Locked"
}
```

**Chat**
```json
{
  "id": "chat-1",
  "title": "Project summary",
  "updatedAt": "2026-07-02T10:15:00.000Z"
}
```

**Message**
```json
{
  "id": "msg-1",
  "role": "assistant",
  "content": "Hello from FastAPI",
  "createdAt": "2026-07-02T10:15:00.000Z",
  "chatId": "chat-1"
}
```

**Document**
```json
{
  "id": "doc-1",
  "name": "Quarterly-notes.pdf",
  "type": "PDF",
  "size": "2.4 MB",
  "updatedAt": "2026-07-02T10:15:00.000Z"
}
```

**Settings**
```json
{
  "endpoint": "/api",
  "model": "Mistral 7B Instruct",
  "temperature": 0.4,
  "topP": "0.9",
  "contextLength": "4096",
  "streamResponses": true,
  "autoSync": true,
  "theme": "dark"
}
```

## Migration steps

**1. Add environment variables**

```bash
VITE_API_BASE_URL=http://localhost:8000
```

**2. Add the Axios client**

Create `src/services/http.js` as shown above.

**3. Replace the demo functions**

Update each function in `src/services/api.js` to call FastAPI instead of returning a static object.

```js
export async function getChats() {
  const response = await http.get('/chats')
  return response.data
}

export async function sendMessage(chatId, message) {
  const response = await http.post(`/chats/${chatId}/messages`, { message })
  return response.data
}
```

This is the low-risk option — the rest of the app is already expecting exactly these function signatures.

**4. Keep store contracts stable**

Don't rename store actions unless you're also updating every component that calls them.

**5. Test page by page**

Go through these in order rather than testing everything at once:

- Chat screen
- Sidebar / recent chats
- Settings page
- Documents page
- Theme switching

## Staying resilient while the backend is still in progress

- Keep the old demo functions around as a fallback.
- Wrap every Axios call in `try/catch`.
- If a request fails, fall back to demo data temporarily instead of breaking the UI.
- Migrate one endpoint at a time — don't flip everything over at once.
- Hold off on UI changes until the API shape has actually settled.

This way the app stays usable even mid-migration, instead of being broken for however long the backend takes to finish.

## Suggested build order

1. `src/services/http.js`
2. `src/services/api.js`
3. `src/store/store.js`
4. `src/components/Chat.jsx` — only if streaming needs extra state
5. `src/pages/Documents.jsx` — only if documents come back differently shaped
6. `src/pages/Settings.jsx` — only if setting keys change

## A note on live chat

For live chat specifically:

- Save the user's message to the store immediately, don't wait on the network.
- The assistant's reply comes back from FastAPI.
- If you're streaming, show a typing indicator until the final message lands.
- If you're not streaming, the existing placeholder-while-waiting approach still works fine.

## Bottom line

The safest path here is to leave the frontend structure alone and swap out just the service layer first. That gets you live backend data with minimal UI churn, an easy rollback if something goes wrong, and a much gentler transition from static demo state to real chats, documents, and settings.