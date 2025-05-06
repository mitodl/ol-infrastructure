import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig, mergeConfig } from '@edx/frontend-platform';
import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';

const configData = getConfig();
const currentYear = new Date().getFullYear();

let userMenu = [
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/dashboard`,
    title: 'Dashboard',
  },
  {
    url: `${process.env.MIT_LEARN_API_BASE_URL}/learn/logout/`,
    title: 'Sign Out',
  },
];

let footerLegalLinks = [
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/about`,
    title: 'About Us',
  },
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/privacy`,
    title: 'Privacy Policy',
  },
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/honor`,
    title: 'Honor Code',
  },
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/terms-of-service`,
    title: 'Terms of Service',
  },
  {
    url: 'https://accessibility.mit.edu/',
    title: 'Accessibility',
  },
];

let copyRightText = 'Massachusetts Institute of Technology';
let logo = <Logo imageUrl={process.env.MIT_LEARN_LOGO} destinationUrl={process.env.MIT_LEARN_BASE_URL} />;
const MITxTCourseKeyFormat = "course-v1:mitxt"

if (window.location.href.toLowerCase().includes(MITxTCourseKeyFormat)) {
  copyRightText = `${configData.SITE_NAME.replace(/\b(CI|QA|Staging)\b/g, "").trim()}. All rights reserved.`;
  logo = <Logo imageUrl={configData.LOGO_URL} destinationUrl={configData.MARKETING_SITE_BASE_URL} />;

  userMenu = [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/dashboard`,
      title: 'Dashboard',
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/profile/`,
      title: 'Profile',
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/account-settings/`,
      title: 'Settings',
    },
    {
      url: `${configData.LMS_BASE_URL}/logout`,
      title: 'Sign Out',
    },
  ];

  footerLegalLinks = [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/about-us/`,
      title: 'About Us',
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/privacy-policy/`,
      title: 'Privacy Policy',
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/honor-code/`,
      title: 'Honor Code',
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/terms-of-service/`,
      title: 'Terms of Service',
    },
    {
      url: 'https://accessibility.mit.edu/',
      title: 'Accessibility',
    },
  ];
}

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

const footerSubSlotsConfig = {
  "frontend.shell.footer.desktop.leftLinks.ui": {
    plugins: [
      { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
      {
        op: PLUGIN_OPERATIONS.Insert,
        widget: {
          id: 'custom_logo',
          type: DIRECT_PLUGIN,
          RenderWidget: () => logo,
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
            <CopyrightNotice copyrightText={`© ${currentYear} ${copyRightText}`} />
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
  if (!window.location.href.toLowerCase().includes(MITxTCourseKeyFormat)) {
    mergeConfig({"LOGO_URL": process.env.MIT_LEARN_LOGO});
  }
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

const modifyLogoHref = ( widget ) => {
  widget.content.href = process.env.MIT_LEARN_BASE_URL;
  widget.content.src = process.env.MIT_LEARN_LOGO;
  return widget;
};

if (!window.location.href.toLowerCase().includes(MITxTCourseKeyFormat)) {
  config.pluginSlots = {
    ...config.pluginSlots,
    logo_slot: {
      keepDefault: true,
      plugins: [
        {
          op: PLUGIN_OPERATIONS.Modify,
          widgetId: 'default_contents',
          fn: modifyLogoHref,
        },
      ]
    },
  }
}

export default config;
