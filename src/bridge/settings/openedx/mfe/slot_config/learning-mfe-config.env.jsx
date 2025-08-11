import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { PLUGIN_OPERATIONS, DIRECT_PLUGIN } from '@openedx/frontend-plugin-framework';
import CourseBreadcrumbs from './src/courseware/course/breadcrumbs';
import { SequenceNavigation } from './src/courseware/course/sequence/sequence-navigation';
import { BookmarkButton } from './src/courseware/course/bookmark';
import messages from './src/courseware/course/sequence/messages';
import { useIntl } from '@edx/frontend-platform/i18n';
import config from './common-mfe-config.env.jsx';

import(
  /**
   * remoteTutorDrawer is already bundled to include its own version
   * of React and ReactDOM.
   *
   * Add webpackIgnore to avoid bundling it again.
   */
  /* webpackIgnore: true */
 "/learn/static/smoot-design/aiDrawerManager.es.js").then(module => {
   module.init({
      messageOrigin: getConfig().LMS_BASE_URL,
      transformBody: messages => ({ message: messages[messages.length - 1].content }),
      getTrackingClient: getAuthenticatedHttpClient,
   })
})

// Configure the MFE plugin slots for the Learning MFE
let learningMFEConfig = {
  ...config,
  pluginSlots: {
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
    }
  },
  
};

export default learningMFEConfig;
