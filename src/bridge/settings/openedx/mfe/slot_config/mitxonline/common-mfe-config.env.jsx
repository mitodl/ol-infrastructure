import { useContext } from "react";
import { AppContext } from '@edx/frontend-platform/react';
import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';

const configData = getConfig();
const UAI_COURSE_KEYS = ['course-v1:uai_'];
const AUTHORING_APP_ID = 'authoring';
const CURRENT_MFE_APP_ID = configData.APP_ID;
const SLOT_IDS = {
  header: {
    learning_user_menu: 'org.openedx.frontend.layout.header_learning_user_menu.v1',
    desktop_user_menu: 'org.openedx.frontend.layout.header_desktop_user_menu.v1',
    learning_course_info: 'org.openedx.frontend.layout.header_learning_course_info.v1',
    learning_user_menu_toggle: 'org.openedx.frontend.layout.header_learning_user_menu_toggle.v1',
    desktop_user_menu_toggle: 'org.openedx.frontend.layout.header_desktop_user_menu_toggle.v1',
  },
  footer: {
    slot: 'footer_slot',
    studio_slot: 'studio_footer_slot',
    desktop_left_links: 'frontend.shell.footer.desktop.leftLinks.ui',
    desktop_center_links: 'frontend.shell.footer.desktop.centerLinks.ui',
    desktop_legal_notices: 'frontend.shell.footer.desktop.legalNotices.ui',
    desktop_top: 'frontend.shell.footer.desktop.top.ui',
    desktop_right_links: 'frontend.shell.footer.desktop.rightLinks.ui',
  },
}

const addFooterSubSlotsOverride = (config) => {
  const currentYear = new Date().getFullYear();
  const accessibilityURL = process.env.ACCESSIBILITY_URL || 'https://accessibility.mit.edu/';
  const contactUsURL = process.env.CONTACT_URL || 'mailto:mitlearn-support@mit.edu';
  const copyRightText = 'Massachusetts Institute of Technology';
  const footerLogo = <Logo imageUrl={configData.LOGO_TRADEMARK_URL} destinationUrl={process.env.MIT_BASE_URL} />;

  const footerLegalLinks = [
    {
      url: `${process.env.MIT_LEARN_BASE_URL}/about`,
      title: 'About Us',
    },
    {
      url: `${process.env.MIT_LEARN_BASE_URL}/terms`,
      title: 'Terms of Service',
    },
    {
      url: accessibilityURL,
      title: 'Accessibility',
    },
    {
      url: contactUsURL,
      title: 'Contact Us',
    },
  ];

  const footerSubSlotsConfig = {
    [SLOT_IDS.footer.desktop_left_links]: {
      plugins: [
        { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
        {
          op: PLUGIN_OPERATIONS.Insert,
          widget: {
            id: 'custom_logo',
            type: DIRECT_PLUGIN,
            RenderWidget: () => footerLogo,
          },
        },
      ],
    },
    [SLOT_IDS.footer.desktop_center_links]: {
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
    [SLOT_IDS.footer.desktop_legal_notices]: {
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
    [SLOT_IDS.footer.desktop_top]: {
      plugins: [{ op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' }],
    },
    [SLOT_IDS.footer.desktop_right_links]: {
      plugins: [{ op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' }],
    },
  };
  return {
    ...config,
    pluginSlots: {
      ...config.pluginSlots,
      ...footerSubSlotsConfig,
    },
  }
}

const addFooterSlotOverride = (config) => {
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

  if (CURRENT_MFE_APP_ID === AUTHORING_APP_ID) {
    return {
      ...config,
      pluginSlots: {
        ...config.pluginSlots,
        [SLOT_IDS.footer.studio_slot]: footerSlotConfig,
      },
    };
  } else {
    return {
      ...config,
      pluginSlots: {
        ...config.pluginSlots,
        [SLOT_IDS.footer.slot]: footerSlotConfig,
      },
    };
  }
}

const isLearnCourse = () => {
  const href = (window.location?.href || document.URL || '').toLowerCase();
  return UAI_COURSE_KEYS.some(key => {
    const encodedKey = encodeURIComponent(key).toLowerCase();
    return href.includes(key) || href.includes(encodedKey)
  });
}

const getUserMenu = () => {
  const userMenuLinkTitles = {
    dashboard: 'Dashboard',
    profile: 'Profile',
    account: 'Settings',
    logout: 'Sign Out',
  };

  if (isLearnCourse()) {
    return [
      {
        url: `${process.env.MIT_LEARN_BASE_URL}/dashboard`,
        title: userMenuLinkTitles.dashboard,
      },
      {
        url: `${configData.LMS_BASE_URL}/logout`,
        title: userMenuLinkTitles.logout,
      },
    ];
  }
  return [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/dashboard`,
      title: userMenuLinkTitles.dashboard,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/profile/`,
      title: userMenuLinkTitles.profile,
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/account-settings/`,
      title: userMenuLinkTitles.account,
    },
    {
      url: `${configData.LMS_BASE_URL}/logout`,
      title: userMenuLinkTitles.logout,
    },
  ];
}

const DesktopHeaderUserMenu = (widget) => {
  const userMenu = getUserMenu();
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
  const userMenu = getUserMenu();
  widget.content.items = userMenu.map((item) => ({
    href: item.url,
    message: item.title,
  }))
  return widget;
};

const displayFullNameInMenu = (widget) => {
  const { authenticatedUser } = useContext(AppContext);
  if (authenticatedUser) {
    widget.content.label = authenticatedUser.name || authenticatedUser.username;
  }
  return widget;
};

const addUserMenuSlotOverride = (config) => {
  const learningApps = ['learning', 'discussions', 'ora-grading', 'communications'];
  const dashboardApps = ['gradebook', 'learner-dashboard'];

  config.pluginSlots = {
    ...config.pluginSlots,
      [SLOT_IDS.header.learning_user_menu_toggle]: {
        keepDefault: true,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Modify,
            widgetId: 'default_contents',
              fn: displayFullNameInMenu,
          },
        ]
      },
      [SLOT_IDS.header.desktop_user_menu_toggle]: {
        keepDefault: true,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Modify,
            widgetId: 'default_contents',
              fn: displayFullNameInMenu,
          },
        ]
      }
  };

  if (learningApps.includes(CURRENT_MFE_APP_ID)) {

    return {
      ...config,
      pluginSlots: {
        ...config.pluginSlots,
        [SLOT_IDS.header.learning_user_menu]: {
          keepDefault: true,
          plugins: [
            {
              op: PLUGIN_OPERATIONS.Modify,
              widgetId: 'default_contents',
              fn: LearningHeaderUserMenu,
            },
          ],
        },
      },
    }
  }
  else if (dashboardApps.includes(CURRENT_MFE_APP_ID)) {

    return {
      ...config,
      pluginSlots: {
        ...config.pluginSlots,
        [SLOT_IDS.header.desktop_user_menu]: {
          keepDefault: true,
          plugins: [
            {
              op: PLUGIN_OPERATIONS.Modify,
              widgetId: 'default_contents',
              fn: DesktopHeaderUserMenu,
            },
          ],
        },
      },
    }
  }
  return config;
}

const addLearningCourseInfoSlotOverride = (config) => {
  if (isLearnCourse()) {
  // Hiding the course org and number from the learning header in the UAI courses
    config.pluginSlots = {
      ...config.pluginSlots,
      [SLOT_IDS.header.learning_course_info]: {
        plugins: [
          { op: PLUGIN_OPERATIONS.Hide, widgetId: 'default_contents' },
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'custom_header_learning_course_info',
              type: DIRECT_PLUGIN,
              RenderWidget: ({ courseTitle }) => (
                <div style={{ paddingTop: '14px', minWidth: 0 }}>
                  <span className='d-block m-0 font-weight-bold course-title'>{courseTitle}</span>
                </div>
              ),
            },
          },
        ],
      },
    };
  }
  return config;
}

const addEnvOverrides = (config) => {
  if (isLearnCourse()) {
    return {
      ...config,
        SUPPORT_URL: 'mailto:mitlearn-support@mit.edu',
    }
  }
  return config;
}

let config = {
  ...process.env,
  // Override the proctoring info panel 'Review instructions and system requirements' link
  externalLinkUrlOverrides : {
    'https://support.edx.org/hc/en-us/sections/115004169247-Taking-Timed-and-Proctored-Exams': 'https://mitxonline.zendesk.com/hc/en-us/articles/4418223178651-What-is-the-Proctortrack-Onboarding-Exam',
  },
};

config = addFooterSubSlotsOverride(config);
config = addFooterSlotOverride(config);
config = addLearningCourseInfoSlotOverride(config);
config = addUserMenuSlotOverride(config);
config = addEnvOverrides(config);

export default config;
