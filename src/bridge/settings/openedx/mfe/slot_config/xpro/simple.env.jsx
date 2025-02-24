import { PLUGIN_OPERATIONS } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';

const modifyUserMenu = (widget) => {
  widget.content.menu = [
    {
      items: [
        {
          type: 'item',
          href: `${getConfig().LMS_BASE_URL}/dashboard`,
          content: 'Dashboard',
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
          type: 'item',
          href: `${getConfig().LMS_BASE_URL}/logout`,
          content: 'Sign Out',
        },
      ],
    },
  ];
  return widget;
};

const config = {
  pluginSlots: {
    desktop_user_menu_slot: {
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
