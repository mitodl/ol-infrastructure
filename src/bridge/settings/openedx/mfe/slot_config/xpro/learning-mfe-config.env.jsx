import { getConfig } from '@edx/frontend-platform';
import config from './common-mfe-config.env.jsx';
import * as remoteTutorDrawer from "./mitodl-smoot-design/dist/bundles/remoteTutorDrawer.umd.js"


remoteTutorDrawer.init({
  messageOrigin: getConfig().LMS_BASE_URL,
  transformBody: messages => ({ message: messages[messages.length - 1].content }),
})

export default config;
