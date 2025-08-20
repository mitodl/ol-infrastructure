import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';

const configData = getConfig();
const currentYear = new Date().getFullYear();
const edxMfeAppName = configData.APP_ID;
const authoringAppID = "authoring";
const href = window.location.href.toLowerCase();
const isLearnCourse = ["course-v1:uai_"].some(key => href.includes(key));
const accessibilityURL = process.env.ACCESSIBILITY_URL || 'https://accessibility.mit.edu/';
const linkTitles = {
  dashboard: "Dashboard",
  profile: "Profile",
  account: "Settings",
  logout: "Sign Out",
  aboutUs: "About Us",
  privacyPolicy: "Privacy Policy",
  honorCode: "Honor Code",
  termsOfService: "Terms of Service",
  accessibility: "Accessibility",
};

const copyRightText = `${configData.SITE_NAME.replace(/\b(CI|QA|Staging)\b/g, "").trim()}. All rights reserved.`;

const logo = <Logo imageUrl={configData.LOGO_URL} destinationUrl={configData.MARKETING_SITE_BASE_URL} />;

let userMenu = [
  {
    url: `${process.env.MIT_LEARN_BASE_URL}/dashboard`,
    title: linkTitles.dashboard,
  },
  {
    url: `${configData.LMS_BASE_URL}/logout`,
    title: linkTitles.logout,
  },
];

if (!isLearnCourse) {

  userMenu = [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/dashboard`,
      title: linkTitles.dashboard,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/profile/`,
      title: linkTitles.profile,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/account-settings/`,
      title: linkTitles.account,
    },
    {
      url: `${configData.LMS_BASE_URL}/logout`,
      title: linkTitles.logout,
    },
  ];

}

const footerLegalLinks = [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/about-us/`,
      title: linkTitles.aboutUs,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/privacy-policy/`,
      title: linkTitles.privacyPolicy,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/honor-code/`,
      title: linkTitles.honorCode,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/terms-of-service/`,
      title: linkTitles.termsOfService,
    },
    {
      url: accessibilityURL,
      title: linkTitles.accessibility,
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
  // Override the proctoring info panel 'Review instructions and system requirements' link
  externalLinkUrlOverrides : {
    "https://support.edx.org/hc/en-us/sections/115004169247-Taking-Timed-and-Proctored-Exams": "https://mitxonline.zendesk.com/hc/en-us/articles/4418223178651-What-is-the-Proctortrack-Onboarding-Exam",
  },
};

// Additional plugin config based on MFE
if (edxMfeAppName === authoringAppID) {
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
