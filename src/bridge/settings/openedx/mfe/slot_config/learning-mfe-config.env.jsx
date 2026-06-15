import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import CourseBreadcrumbs from './src/courseware/course/breadcrumbs';
import { SequenceNavigation } from './src/courseware/course/sequence/sequence-navigation';
import { BookmarkButton } from './src/courseware/course/bookmark';
import messages from './src/courseware/course/sequence/messages';
import { useIntl } from '@edx/frontend-platform/i18n';
import config from './common-mfe-config.env.jsx';
import SidebarAIDrawerCoordinator from './SidebarAIDrawerCoordinator.jsx';
import ResponsiveCourseTabs from './ResponsiveCourseTabs.jsx';
import FeedbackDrawerManagerSidebar from './FeedbackDrawerManagerSidebar.jsx';
import FeedbackLauncherSlot from './FeedbackLauncherSlot.jsx';

// When ENABLE_AI_DRAWER_SLOT is disabled or unset
const ENABLE_AI_DRAWER_SLOT = process.env.ENABLE_AI_DRAWER_SLOT === "true";
// Per-block feedback (in-iframe megaphone -> drawer).
const ENABLE_FEEDBACK_SLOT = process.env.ENABLE_FEEDBACK_SLOT === "true";
// When true, per-block feedback renders inline in the AskTIM sidebar column
// (handled by SidebarAIDrawerCoordinator); when false it uses the overlay loader.
const FEEDBACK_SLOT_MODE = process.env.FEEDBACK_SLOT_MODE === "true";
// Generic/per-unit feedback: floating three-reaction launcher in learner_tools.
const ENABLE_FEEDBACK_LAUNCHER = process.env.ENABLE_FEEDBACK_LAUNCHER === "true";

if (!ENABLE_AI_DRAWER_SLOT) {
  import(
    /**
     * remoteTutorDrawer is already bundled to include its own version
     * of React and ReactDOM.
     *
     * Add webpackIgnore to avoid bundling it again.
     */
    /* webpackIgnore: true */
    "/learn/static/smoot-design/aiDrawerManager.es.js"
  ).then(module => {
    module.init({
      messageOrigin: getConfig().LMS_BASE_URL,
      transformBody: messages => ({ message: messages[messages.length - 1].content }),
      getTrackingClient: getAuthenticatedHttpClient,
    });
  });
}

let learningMFEConfig = {
    ...config
}

// Configure the Breadcrumbs old behaviour in MITx Online Learning MFE
if (process.env.DEPLOYMENT_NAME?.includes("mitxonline")) {
  learningMFEConfig.pluginSlots = {
      ...config.pluginSlots,
      // Render the CourseBreadcrumbs component in the course_breadcrumbs slot
      'org.openedx.frontend.learning.course_breadcrumbs.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'default_breadcrumbs_component',
              type: DIRECT_PLUGIN,
              RenderWidget: ({ courseId, sectionId, sequenceId, isStaff, unitId }) => (
                <CourseBreadcrumbs
                  courseId={courseId}
                  sectionId={sectionId}
                  sequenceId={sequenceId}
                  isStaff={isStaff}
                  unitId={unitId}
                />
              ),
            },
          },
        ]
      },
      // Render the SequenceNavigation component in the sequence_navigation slot
      'org.openedx.frontend.learning.sequence_navigation.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'custom_sequence_navigation',
              type: DIRECT_PLUGIN,
              RenderWidget: ({ sequenceId, unitId, nextHandler, onNavigate, previousHandler }) => (
                <SequenceNavigation
                  sequenceId={sequenceId}
                  unitId={unitId}
                  nextHandler={nextHandler}
                  onNavigate={onNavigate}
                  previousHandler={previousHandler}
                />
              ),
            },
          },
        ],
      },
      // Hide the default course outline sidebar
      'org.openedx.frontend.learning.course_outline_sidebar.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Hide,
            widgetId: 'default_contents',
          },
        ]
      },
      // The unit title slot includes navigation arrow buttons that aren’t needed,
      // so we render a custom unit title component instead.
      'org.openedx.frontend.learning.unit_title.v1': {
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'custom_unit_title_content',
              type: DIRECT_PLUGIN,
              RenderWidget: ({ unit }) => {
              const isProcessing = unit.bookmarkedUpdateState === 'loading';
              const {formatMessage} = useIntl();
              return <>
                  <div className="d-flex justify-content-between">
                      <div className="mb-0">
                          <h3 className="h3">{unit.title}</h3>
                      </div>
                  </div>
                  <p className="sr-only">{formatMessage(messages.headerPlaceholder)}</p>
                  <BookmarkButton
                      unitId={unit.id}
                      isBookmarked={unit.bookmarked}
                      isProcessing={isProcessing}
                  />
              </>
              },
            },
          },
        ]
      },
      'org.openedx.frontend.learning.course_outline_sidebar_trigger.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Hide,
            widgetId: 'default_trigger',
          },
        ]
      },
      'org.openedx.frontend.learning.course_outline_mobile_sidebar_trigger.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Hide,
            widgetId: 'default_trigger',
          },
        ]
      },

      // Replace default tab links with responsive overflow tabs.
      'org.openedx.frontend.learning.course_tab_links.v1': {
        keepDefault: false,
        plugins: [
          {
            op: PLUGIN_OPERATIONS.Insert,
            widget: {
              id: 'responsive_course_tabs',
              type: DIRECT_PLUGIN,
              priority: 1,
              RenderWidget: ({ activeTabSlug }) => (
                <ResponsiveCourseTabs activeTabSlug={activeTabSlug} />
              ),
            },
          },
        ],
      },
      // Sidebar slot: AskTIM AI drawer and/or per-block feedback drawer (flag-gated).
      // Both share this one slot, so they're merged into a single definition to
      // avoid the later key silently overwriting the earlier one.
      ...((ENABLE_AI_DRAWER_SLOT || ENABLE_FEEDBACK_SLOT) ? {
        'org.openedx.frontend.learning.notifications_discussions_sidebar.v1': {
            keepDefault: false,
            plugins: [
                // Coordinator owns the sidebar column. Mount it for AskTIM, and also
                // when feedback runs in slot mode (it renders feedback inline there).
                ...((ENABLE_AI_DRAWER_SLOT || (ENABLE_FEEDBACK_SLOT && FEEDBACK_SLOT_MODE)) ? [{
                    op: PLUGIN_OPERATIONS.Insert,
                    widget: {
                        id: 'coordinated_sidebar_with_ai_drawer',
                        type: DIRECT_PLUGIN,
                        RenderWidget: ({ courseId }) => <SidebarAIDrawerCoordinator courseId={courseId} />,
                    },
                }] : []),
                // Overlay loader only when feedback is enabled and NOT in slot mode.
                ...((ENABLE_FEEDBACK_SLOT && !FEEDBACK_SLOT_MODE) ? [{
                    op: PLUGIN_OPERATIONS.Insert,
                    widget: {
                        id: 'feedback_drawer_loader',
                        type: DIRECT_PLUGIN,
                        RenderWidget: () => <FeedbackDrawerManagerSidebar />,
                    },
                }] : []),
            ],
        },
      } : {}),

      // Generic/per-unit feedback: floating three-reaction launcher mounted in the
      // learner_tools slot (portals to document.body; desktop/tablet only).
      ...(ENABLE_FEEDBACK_LAUNCHER ? {
        'org.openedx.frontend.learning.learner_tools.v1': {
            plugins: [
                {
                    op: PLUGIN_OPERATIONS.Insert,
                    widget: {
                        id: 'feedback_reaction_launcher',
                        type: DIRECT_PLUGIN,
                        RenderWidget: ({ courseId, unitId }) => (
                            <FeedbackLauncherSlot courseId={courseId} unitId={unitId} />
                        ),
                    },
                },
            ],
        },
      } : {}),
  };
}


export default learningMFEConfig;
