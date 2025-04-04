import { getConfig } from '@edx/frontend-platform';
import * as remoteTutorDrawer from "./mitodl-smoot-design/dist/bundles/remoteTutorDrawer.umd.js"


remoteTutorDrawer.init({
  messageOrigin: getConfig().LMS_BASE_URL,
  transformBody: messages => ({ message: messages[messages.length - 1].content }),
})

const config = {
  ...process.env
};
  
export default config;
