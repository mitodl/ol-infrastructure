import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';

const configData = getConfig();
const currentYear = new Date().getFullYear();

const userMenu = {
    dashboard: {
        url: `${configData.LMS_BASE_URL}/dashboard`,
        title: 'Dashboard',
    },
    profile: {
        url: `${configData.MARKETING_SITE_BASE_URL}/profile/`,
        title: 'Profile',
    },
    settings: {
        url: `${configData.MARKETING_SITE_BASE_URL}/account-settings/`,
        title: 'Settings',
    },
    logout: {
        url: `${configData.LMS_BASE_URL}/logout`,
        title: 'Sign Out',
    },
}

const DesktopHeaderUserMenu = (widget) => {
  widget.content.menu = [
    {
      items: [
        {
          type: 'item',
          href: userMenu.dashboard.url,
          content: userMenu.dashboard.title,
        },
        {
          type: 'item',
          href: userMenu.profile.url,
          content: userMenu.profile.title,
        },
        {
          type: 'item',
          href: userMenu.settings.url,
          content: userMenu.settings.title,
        },
        {
          type: 'item',
          href:  userMenu.logout.url,
          content: userMenu.logout.title,
        },
      ],
    },
  ];
  return widget;
};

const LearningHeaderUserMenu = (widget) => {
  widget.content.items = [
    {
      href: userMenu.dashboard.url,
      message: userMenu.dashboard.title,
    },
    {
      href: userMenu.profile.url,
      message: userMenu.profile.title,
    },
    {
      href: userMenu.settings.url,
      message: userMenu.settings.title,
    },
    {
      href: userMenu.logout.url,
      message: userMenu.logout.title,
    },
  ];
  return widget;
};

const footerSubSlotsConfig = {
  "frontend.shell.footer.desktop.leftLinks.ui": {
    plugins: [
      { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
      {
        op: PLUGIN_OPERATIONS.Insert,
        widget: {
          id: 'custom_logo',
          type: DIRECT_PLUGIN,
          RenderWidget: () => (
            <Logo imageUrl={configData.LOGO_URL} destinationUrl={configData.MARKETING_SITE_BASE_URL} />
          ),
        },
      },
    ],
  },
  "frontend.shell.footer.desktop.centerLinks.ui": {
    plugins: [
      { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
      {
        op: PLUGIN_OPERATIONS.Insert,
        widget: {
          id: 'custom_menu_links',
          type: DIRECT_PLUGIN,
          RenderWidget: () => (
            <MenuLinks marketingSiteUrl={configData.MARKETING_SITE_BASE_URL} siteName={configData.SITE_NAME} />
          ),
        },
      },
    ],
  },
  "frontend.shell.footer.desktop.legalNotices.ui": {
    plugins: [
      { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
      {
        op: PLUGIN_OPERATIONS.Insert,
        widget: {
          id: 'custom_legal_notice',
          type: DIRECT_PLUGIN,
          RenderWidget: () => (
            <CopyrightNotice copyrightText={`© ${currentYear} ${configData.SITE_NAME.replace(/\b(CI|QA|Staging)\b/g, "").trim()}. All rights reserved.`} />
          ),
        },
      },
    ],
  },
  "frontend.shell.footer.desktop.top.ui": {
    plugins: [{ op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' }],
  },
  "frontend.shell.footer.desktop.rightLinks.ui": {
    plugins: [{ op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' }],
  },
};

const footerSlotConfig = {
  plugins: [
    { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
    {
      op: PLUGIN_OPERATIONS.Insert,
      widget: {
        id: 'custom_footer',
        type: DIRECT_PLUGIN,
        RenderWidget: () => <Footer />,
      },
    },
  ],
};

let config = {
  ...process.env,
  pluginSlots: footerSubSlotsConfig,
};

// Additional plugin config based on MFE
const edxMfeAppName = configData.APP_ID;

if (edxMfeAppName === "authoring") {
  config = {
    ...config,
    pluginSlots: {
      studio_footer_slot: footerSlotConfig,
      ...config.pluginSlots,
    },
  };
} else {
  config = {
    ...config,
    pluginSlots: {
      footer_slot: footerSlotConfig,
      ...config.pluginSlots,
    },
  };
}

// Dynamic header menu slot overrides
const learningApps = ["learning", "discussions", "ora-grading", "communications"];
const dashboardApps = ["gradebook", "learner-dashboard"];

if (learningApps.includes(edxMfeAppName)) {
  config.pluginSlots.learning_user_menu_slot = {
    keepDefault: true,
    plugins: [
      {
        op: PLUGIN_OPERATIONS.Modify,
        widgetId: 'default_contents',
        fn: LearningHeaderUserMenu,
      },
    ],
  };
} else if (dashboardApps.includes(edxMfeAppName)) {
  config.pluginSlots.desktop_user_menu_slot = {
    keepDefault: true,
    plugins: [
      {
        op: PLUGIN_OPERATIONS.Modify,
        widgetId: 'default_contents',
        fn: DesktopHeaderUserMenu,
      },
    ],
  };
}

export default config;
