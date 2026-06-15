import { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { loadBundle, SUBMIT_URL } from './feedbackBundle';
import useFeedbackEnrichment from './useFeedbackEnrichment';

/**
 * Third feedback mode: a floating three-reaction launcher mounted via the
 * `learner_tools.v1` slot (which portals to document.body and is desktop/tablet
 * only). Clicking a reaction opens the FeedbackDrawer pre-selected to that
 * sentiment.
 *
 * The slot only knows the course + unit (not an individual block), so feedback
 * raised here is UNIT-scoped: blockUsageKey is the unit id and blockType is
 * "vertical". A payload getter keeps this fresh across unit navigation so the
 * bundle is initialized only once.
 */
const FeedbackLauncherSlot = ({ courseId, unitId }) => {
  const instanceRef = useRef(null);
  const payloadRef = useRef({});
  const getEnrichment = useFeedbackEnrichment(courseId, unitId);

  // Keep the latest unit context available to the (once-initialized) bundle.
  payloadRef.current = {
    courseId,
    blockUsageKey: unitId,
    blockType: 'vertical',
    blockDisplayName: '',
    ...getEnrichment(),
  };

  useEffect(() => {
    let isMounted = true;
    loadBundle()
      .then((module) => {
        if (!isMounted) {
          return;
        }
        instanceRef.current = module.init({
          mode: 'launcher',
          getPayload: () => payloadRef.current,
          submitUrl: SUBMIT_URL,
          getSubmitClient: getAuthenticatedHttpClient,
        });
      })
      .catch(() => { /* bundle load/init failure is non-fatal for the page */ });

    return () => {
      isMounted = false;
      if (instanceRef.current?.unmount) {
        try {
          instanceRef.current.unmount();
        } catch (error) { /* unmount handles its own errors */ }
      }
      instanceRef.current = null;
    };
  }, []);

  return null;
};

FeedbackLauncherSlot.propTypes = {
  courseId: PropTypes.string.isRequired,
  unitId: PropTypes.string.isRequired,
};

export default FeedbackLauncherSlot;
