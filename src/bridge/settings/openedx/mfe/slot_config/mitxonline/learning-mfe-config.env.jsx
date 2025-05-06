import config from './common-mfe-config.env.jsx';

import(
  /**
   * remoteTutorDrawer is already bundled to include its own version
   * of React and ReactDOM.
   *
   * Add webpackIgnore to avoid bundling it again.
   */
  /* webpackIgnore: true */
 "/static/smoot-design/remoteTutorDrawer.es.js").then(module => {
   module.init({
     messageOrigin: "http://local.openedx.io:8000",
     transformBody: messages => ({ message: messages[messages.length - 1].content }),
   })
})

export default config;
