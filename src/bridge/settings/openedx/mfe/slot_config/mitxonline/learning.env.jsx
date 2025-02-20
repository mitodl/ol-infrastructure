import { PLUGIN_OPERATIONS } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';

const modifyUserMenu = (widget) => {
  widget.content.items = [
    {
      href: `${getConfig().LMS_BASE_URL}/dashboard`,
      message: 'Dashboard',
    },
    {
      href: `${getConfig().MARKETING_SITE_BASE_URL}/profile/`,
      message: 'Profile',
    },
    {
      href: `${getConfig().MARKETING_SITE_BASE_URL}/account-settings/`,
      message: 'Settings',
    },
    {
      href: `${getConfig().LMS_BASE_URL}/logout`,
      message: 'Sign Out',
    },
  ];
  return widget;
};

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
