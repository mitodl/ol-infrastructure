import { getConfig } from '@edx/frontend-platform';

export const BUNDLE_PATH = process.env.FEEDBACK_DRAWER_BUNDLE_PATH
  || '/learn/static/smoot-design/feedbackDrawerManager.es.js';

// learn-ai endpoint; unset = the bundle treats submit as a no-op success (dev stub).
export const SUBMIT_URL = process.env.FEEDBACK_SUBMIT_URL || undefined;

let loadPromise = null;
export const loadBundle = () => {
  if (loadPromise) {
    return loadPromise;
  }
  loadPromise = import(/* webpackIgnore: true */ BUNDLE_PATH)
    .then((module) => {
      if (!module?.init) {
        throw new Error('feedbackDrawerManager bundle has no init()');
      }
      return module;
    })
    .catch((error) => {
      loadPromise = null;
      throw error;
    });
  return loadPromise;
};

export const getMessageOrigin = () => {
  const lmsBaseUrl = getConfig().LMS_BASE_URL;
  try {
    return new URL(lmsBaseUrl).origin;
  } catch (error) {
    return window.location.origin;
  }
};
