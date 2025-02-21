import { PLUGIN_OPERATIONS } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';

import * as remoteAiChatDrawer from "./mitodl-smoot-design/dist/bundles/remoteAiChatDrawer.umd.js"


const modifyUserMenu = (widget) => {
  widget.content.items = [
    {
      href: `${getConfig().LMS_BASE_URL}/dashboard`,
      message: 'Dashboard',
    },
    {
      href: `${getConfig().LMS_BASE_URL}/logout`,
      message: 'Sign Out',
    },
  ];
  return widget;
};

if (getConfig().APP_ID === 'learning') {
  remoteAiChatDrawer.init({
    messageOrigin: getConfig().LMS_BASE_URL,
    transformBody: messages => ({ message: messages[messages.length - 1].content }),
  })
}

const config = {
  pluginSlots: {
    learning_user_menu_slot: {
      keepDefault: true,
      plugins: [
        {
          op: PLUGIN_OPERATIONS.Modify,
          widgetId: 'default_contents',
          fn: modifyUserMenu,
        },
      ],
    },
  },
};

export default config;
