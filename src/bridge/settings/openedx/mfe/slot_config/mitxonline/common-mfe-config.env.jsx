import { useContext } from "react";
import { AppContext } from '@edx/frontend-platform/react';
import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import { FormattedMessage } from '@edx/frontend-platform/i18n';

import Footer, { Logo, MenuLinks, CopyrightNotice } from './Footer.jsx';
import './mitxonline-styles.scss';

const configData = getConfig();
const UAI_COURSE_KEYS = ['course-v1:uai_'];
const MOBILE_BREAKPOINT = 991; // px
const AUTHORING_APP_ID = 'authoring';
const LEARNING_APPS = ['learning', 'discussions', 'ora-grading', 'communications'];
const DASHBOARD_APPS = ['gradebook', 'learner-dashboard'];

const CURRENT_MFE_APP_ID = configData.APP_ID;
const SLOT_IDS = {
  header: {
    learning_user_menu: 'org.openedx.frontend.layout.header_learning_user_menu.v1',
    desktop_user_menu: 'org.openedx.frontend.layout.header_desktop_user_menu.v1',
    learning_course_info: 'org.openedx.frontend.layout.header_learning_course_info.v1',
    learning_user_menu_toggle: 'org.openedx.frontend.layout.header_learning_user_menu_toggle.v1',
    desktop_user_menu_toggle: 'org.openedx.frontend.layout.header_desktop_user_menu_toggle.v1',
    logo: 'org.openedx.frontend.layout.header_logo.v1',
    learning_help: 'org.openedx.frontend.layout.header_learning_help.v1',
    desktop_secondary_user_menu: 'org.openedx.frontend.layout.header_desktop_secondary_menu.v1',
    desktop_main_menu_slot: 'org.openedx.frontend.layout.header_desktop_main_menu.v1',
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
  const supportURL = process.env.SUPPORT_URL || 'https://mitxonline.zendesk.com/hc/en-us';
  const footerLogo = <Logo imageUrl={configData.LOGO_TRADEMARK_URL} destinationUrl={process.env.MIT_BASE_URL} />;

  const footerLegalLinks = [
    {
      url: `${process.env.MIT_LEARN_BASE_URL}/about`,
      title: 'About Us',
      messageId: 'footer.links.about.us',
    },
    {
      url: `${process.env.MIT_LEARN_BASE_URL}/terms`,
      title: 'Terms of Service',
      messageId: 'footer.links.terms.of.service',
    },
    {
      url: accessibilityURL,
      title: 'Accessibility',
      messageId: 'footer.links.accessibility',
    },
    {
      url: contactUsURL,
      title: 'Contact Us',
      messageId: 'footer.links.contact.us',
    },
  ]

  if (!isLearnCourse()) {
    footerLegalLinks.push(
      {
        url: supportURL,
        title: 'Help',
        messageId: 'footer.links.help',
      }
    );
  }

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
              <CopyrightNotice
                copyrightText={`© ${currentYear} ${copyRightText}`}
                trademarkMessageId="footer.trademark.notice"
                currentYear={currentYear}
              />
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

const isMobile = () => {
  // Guard for SSR / tests
  if (typeof window === 'undefined') {
    return false;
  }
  return window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`).matches;
};

const getUserMenu = ({ includeDashboard = false } = {}) => {
  // Use translation message IDs
  const userMenuLinkMessages = {
    profile: { id: 'header.user.menu.profile', defaultMessage: 'Profile' },
    account: { id: 'header.user.menu.account.setting', defaultMessage: 'Settings' }, // Custom key for MITx Online
    logout: { id: 'header.user.menu.signout', defaultMessage: 'Sign out' }, // Custom key for MITx Online
    dashboard: { id: 'header.user.menu.dashboard', defaultMessage: 'Dashboard' },
  };

  // Build dashboard URL consistently with SecondaryMenu logic
  let dashboardURL = process.env.MIT_LEARN_BASE_URL ? `${process.env.MIT_LEARN_BASE_URL}/dashboard` : 'https://learn.mit.edu/dashboard';
  if (!isLearnCourse()) {
    dashboardURL = configData.MARKETING_SITE_BASE_URL ? `${configData.MARKETING_SITE_BASE_URL}/dashboard/` : 'https://mitxonline.mit.edu/dashboard/';
  }

  if (isLearnCourse()) {
    const baseMenu = [
      {
        url: `${configData.LMS_BASE_URL}/logout`,
        message: (
          <FormattedMessage
            id={userMenuLinkMessages.logout.id}
            defaultMessage={userMenuLinkMessages.logout.defaultMessage}
          />
        ),
      },
    ];
    return includeDashboard ? [{
      url: dashboardURL,
      message: (
        <FormattedMessage
          id={userMenuLinkMessages.dashboard.id}
          defaultMessage={userMenuLinkMessages.dashboard.defaultMessage}
        />
      ),
    }, ...baseMenu] : baseMenu;
  }

  const baseMenu = [
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/profile/`,
      message: (
        <FormattedMessage
          id={userMenuLinkMessages.profile.id}
          defaultMessage={userMenuLinkMessages.profile.defaultMessage}
        />
      ),
    },
    {
      url: `${configData.MARKETING_SITE_BASE_URL}/account-settings/`,
      message: (
        <FormattedMessage
          id={userMenuLinkMessages.account.id}
          defaultMessage={userMenuLinkMessages.account.defaultMessage}
        />
      ),
    },
    {
      url: `${configData.LMS_BASE_URL}/logout`,
      message: (
        <FormattedMessage
          id={userMenuLinkMessages.logout.id}
          defaultMessage={userMenuLinkMessages.logout.defaultMessage}
        />
      ),
    },
  ];
  return includeDashboard ? [{
    url: dashboardURL,
    message: (
      <FormattedMessage
        id={userMenuLinkMessages.dashboard.id}
        defaultMessage={userMenuLinkMessages.dashboard.defaultMessage}
      />
    ),
  }, ...baseMenu] : baseMenu;
};

const DesktopHeaderUserMenu = (widget) => {
  const userMenu = getUserMenu({ includeDashboard: isMobile() });
  widget.content.menu = [
    {
      items: userMenu.map((item) => ({
        type: 'item',
        href: item.url,
        content: item.message,
      })),
    },
  ];
  return widget;
};

const LearningHeaderUserMenu = (widget) => {
  const userMenu = getUserMenu({ includeDashboard: isMobile() });
  widget.content.items = userMenu.map((item) => ({
    href: item.url,
    message: item.message,
  }))
  return widget;
};

const UserMenuOverride = () => {
  const { authenticatedUser } = useContext(AppContext);
  if (!authenticatedUser) {
    return null;
  }
  return (
    <>
      <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 32 32" fill="none"><path d="M15.9998 2.66797C23.3598 2.66797 29.3332 8.6413 29.3332 16.0013C29.3332 23.3613 23.3598 29.3346 15.9998 29.3346C8.63984 29.3346 2.6665 23.3613 2.6665 16.0013C2.6665 8.6413 8.63984 2.66797 15.9998 2.66797ZM8.03093 20.5564C9.98761 23.4772 12.9267 25.3346 16.2128 25.3346C19.4989 25.3346 22.438 23.4772 24.3946 20.5564C22.2512 18.5576 19.3748 17.3346 16.2128 17.3346C13.0508 17.3346 10.1744 18.5576 8.03093 20.5564ZM15.9998 14.668C18.209 14.668 19.9998 12.8771 19.9998 10.668C19.9998 8.45884 18.209 6.66797 15.9998 6.66797C13.7906 6.66797 11.9998 8.45884 11.9998 10.668C11.9998 12.8771 13.7906 14.668 15.9998 14.668Z" fill="white"></path></svg>
      {/* Username hidden on mobile via CSS; remains for larger screens */}
      <span className="user-menu-name">{authenticatedUser.name || authenticatedUser.username}</span>
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" width="24" height="24" fill="currentColor" class="remixicon "><path d="M11.9999 13.1714L16.9497 8.22168L18.3639 9.63589L11.9999 15.9999L5.63599 9.63589L7.0502 8.22168L11.9999 13.1714Z"></path></svg>
    </>
  );
};

const addUserMenuSlotOverride = (config) => {
  config.pluginSlots = {
    ...config.pluginSlots,
      [SLOT_IDS.header.learning_user_menu_toggle]: {
        keepDefault: true,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Hide,
            widgetId: 'default_contents'
          },
          {
          op: PLUGIN_OPERATIONS.Insert,
          widget: {
            id: 'custom_learning_user_menu_toggle',
            type: DIRECT_PLUGIN,
            RenderWidget: () => <UserMenuOverride />
          },
        },
        ]
      },
      [SLOT_IDS.header.desktop_user_menu_toggle]: {
        keepDefault: true,
        plugins: [
          {
          op: PLUGIN_OPERATIONS.Hide,
          widgetId: 'default_contents'
          },
          {
          op: PLUGIN_OPERATIONS.Insert,
          widget: {
            id: 'custom_learning_user_menu_toggle',
            type: DIRECT_PLUGIN,
            RenderWidget: () => <UserMenuOverride />
          },
        },
        ]
      }
  };

  if (LEARNING_APPS.includes(CURRENT_MFE_APP_ID)) {

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
  else if (DASHBOARD_APPS.includes(CURRENT_MFE_APP_ID)) {

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
                <div style={{ paddingTop: '7px', minWidth: 0 }}>
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

const modifyLogoHref = ( widget ) => {
  if (isLearnCourse()) {
    widget.content.href = `${process.env.MIT_LEARN_BASE_URL}/dashboard` || "https://learn.mit.edu/dashboard";
  } else {
    widget.content.href = `${configData.MARKETING_SITE_BASE_URL}/dashboard/` || "https://mitxonline.mit.edu/dashboard/";
  }
  return widget;
};

const addLogoSlotOverride = (config) => {
  return  {
    ...config,
    pluginSlots: {
      ...config.pluginSlots,
      [SLOT_IDS.header.logo]: {
        keepDefault: true,
        plugins: [
            {
              op: PLUGIN_OPERATIONS.Modify,
              widgetId: 'default_contents',
              fn: modifyLogoHref,
            },
        ]
      },
    },
  }
}

const SecondaryMenu = () => {
  let dashboardURL = process.env.MIT_LEARN_BASE_URL ? `${process.env.MIT_LEARN_BASE_URL}/dashboard` : 'https://learn.mit.edu/dashboard';
  if (!isLearnCourse()) {
    dashboardURL = configData.MARKETING_SITE_BASE_URL ? `${configData.MARKETING_SITE_BASE_URL}/dashboard/` : 'https://mitxonline.mit.edu/dashboard/';
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
    }}>
      <a
        href={dashboardURL}
        className="dashboard-btn"
      >
        <FormattedMessage
          id="header.menu.dashboard.label"
          defaultMessage="Dashboard"
        />
      </a>
    </div>
  );
}

const addLearningHelpSlotOverride = (config) => {
  if (!LEARNING_APPS.includes(CURRENT_MFE_APP_ID)) {
    return config;
  }

  return {
    ...config,
    pluginSlots: {
      ...config.pluginSlots,
      [SLOT_IDS.header.learning_help]: {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'custom_learning_help',
              type: DIRECT_PLUGIN,
              RenderWidget: () => <SecondaryMenu />
            }
          }
        ]
      }
    }
  }
}

const addSecondaryMenuSlotOverride = (config) => {
  if (!DASHBOARD_APPS.includes(CURRENT_MFE_APP_ID)) {
    return config;
  }

  return {
    ...config,
    pluginSlots: {
      ...config.pluginSlots,
      [SLOT_IDS.header.desktop_secondary_user_menu]: {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'custom_secondary_menu_component',
              type: DIRECT_PLUGIN,
              RenderWidget: () => <div style={{marginRight: '16px'}}><SecondaryMenu /></div>
            }
          }
        ]
      }
    }
  }
}

const addEnvOverrides = (config) => {
  if (CURRENT_MFE_APP_ID === AUTHORING_APP_ID) {
    config = {
      ...config,
      LOGO_URL: process.env.LOGO_URL.replace(/logo\.svg$/, 'old-logo.svg'),
    }
  }
  if (isLearnCourse()) {
    return {
      ...config,
      SUPPORT_URL: process.env.CONTACT_URL || 'mailto:mitlearn-support@mit.edu',
    }
  }
  return config;
}

const addDesktopMainMenuSlotOverride = (config) => {
  if (!DASHBOARD_APPS.includes(CURRENT_MFE_APP_ID)) {
    return config;
  }

  return {
    ...config,
    pluginSlots: {
      ...config.pluginSlots,
      [SLOT_IDS.header.desktop_main_menu_slot]: {
        keepDefault: true,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Hide,
            widgetId: 'default_contents',
          },
        ]
      }
    },
  }
}

const removeScheduleAndDetailsPageBannerDefaultContents = (config) => {
  config.pluginSlots = {
    ...config.pluginSlots,
    'org.openedx.frontend.authoring.page_banner.v1': {
      plugins: [
        {
          op: PLUGIN_OPERATIONS.Hide,
          widgetId: 'default_contents',
        },
      ],
    },
  };
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
config = addLogoSlotOverride(config);
config = addLearningHelpSlotOverride(config);
config = addSecondaryMenuSlotOverride(config);
config = addDesktopMainMenuSlotOverride(config);
config = removeScheduleAndDetailsPageBannerDefaultContents(config);
config = addEnvOverrides(config);

export default config;
