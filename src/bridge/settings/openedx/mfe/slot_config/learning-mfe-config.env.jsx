import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import config from './common-mfe-config.env.jsx';

import(
  /**
   * remoteTutorDrawer is already bundled to include its own version
   * of React and ReactDOM.
   *
   * Add webpackIgnore to avoid bundling it again.
   */
  /* webpackIgnore: true */
 "/learn/static/smoot-design/aiDrawerManager.es.js").then(module => {
   module.init({
      messageOrigin: getConfig().LMS_BASE_URL,
      transformBody: messages => ({ message: messages[messages.length - 1].content }),
      getTrackingClient: getAuthenticatedHttpClient,
   })
})

export default config;
