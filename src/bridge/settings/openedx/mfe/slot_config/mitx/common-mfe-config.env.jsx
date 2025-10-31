import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';
import styles from './mitx-styles.scss';

const configData = getConfig();
const currentYear = new Date().getFullYear();

const userMenu = [
  {
    url: `${configData.LMS_BASE_URL}/dashboard`,
    title: 'Dashboard',
  },
  {
    url: `${configData.LMS_BASE_URL}/logout`,
    title: 'Sign Out',
  },
];

const DesktopHeaderUserMenu = (widget) => {
  widget.content.menu = [
    {
      items: userMenu.map((item) => ({
        type: 'item',
        href: item.url,
        content: item.title,
      })),
    },
  ];
  return widget;
};

const LearningHeaderUserMenu = (widget) => {
  widget.content.items = userMenu.map((item) => ({
    href: item.url,
    message: item.title,
  }))
  return widget;
};

const footerLegalLinks = [
  {
    url: `${configData.MARKETING_SITE_BASE_URL}/tos/`,
    title: 'Terms of Service',
  },
  {
    url: 'https://accessibility.mit.edu/',
    title: 'Accessibility',
  },
];

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
            <Logo imageUrl={process.env.LOGO_TRADEMARK_URL} destinationUrl={process.env.MIT_OPEN_LEARNING_SITE_LINK} />
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
            <MenuLinks menuItems={footerLegalLinks} />
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

// Removes the looking for a new challenge banner from the Learner Dashboard MFE sidebar
config.pluginSlots.widget_sidebar_slot = {
  plugins: [{ op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' }]
}

export default config;
