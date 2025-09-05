import config from './common-mfe-config.env.jsx';

// We use postMessage to communicate between the iframe and the parent window.
// Listen for messages from xBlock Asides to trigger a course refresh when Aside state changes.
// Uses localStorage and the StorageEvent to notify the authoring MFE to refresh the course data.
window.addEventListener("message", function (event) {
  // Optional: check origin
  if (event.origin !== configData.STUDIO_BASE_URL) return;

  if (event.data?.type === "COURSE_REFRESH_TRIGGER") {
    const storageKey = 'courseRefreshTriggerOnComponentEditSave';
    localStorage.setItem(storageKey, Date.now());

    window.dispatchEvent(new StorageEvent('storage', {
      key: storageKey,
      newValue: Date.now().toString(),
    }));
  }
});

export default config;
