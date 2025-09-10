import { getConfig } from '@edx/frontend-platform';

import config from './common-mfe-config.env.jsx';

// We use postMessage to communicate between the iframe and the parent window.
// Listen for messages from xBlock Asides to trigger a course refresh when Aside state changes.
// Uses localStorage and the StorageEvent to notify the authoring MFE to refresh the course data.
window.addEventListener("message", function (event) {
  if (event.origin !== getConfig().STUDIO_BASE_URL) return;

  if (event.data?.type === "COURSE_REFRESH_TRIGGER") {
    // Authoring MFE listens for changes to this key to trigger a refresh of the course data.
    const storageKey = 'courseRefreshTriggerOnComponentEditSave';
    localStorage.setItem(storageKey, Date.now());

    window.dispatchEvent(new StorageEvent('storage', {
      key: storageKey,
      newValue: Date.now().toString(),
    }));
  }
});

export default config;
