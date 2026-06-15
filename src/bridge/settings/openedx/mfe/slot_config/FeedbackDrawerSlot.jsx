import { useContext, useEffect, useRef } from 'react';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import SidebarContext from './src/courseware/course/sidebar/SidebarContext';
import { loadBundle, getMessageOrigin, SUBMIT_URL } from './feedbackBundle';
import useFeedbackEnrichment from './useFeedbackEnrichment';

/**
 * Slot-mode feedback loader: initializes the smoot-design feedbackDrawerManager
 * bundle with `variant: 'slot'` into a container so the feedback form renders
 * INLINE in the courseware sidebar column (the same region AskTIM uses), rather
 * than as a right-side overlay. Visibility/coordination is owned by
 * SidebarAIDrawerCoordinator; this component just hosts the bundle.
 */
const FeedbackDrawerSlot = () => {
  const { courseId = null, unitId = null } = useContext(SidebarContext) ?? {};
  const getEnrichment = useFeedbackEnrichment(courseId, unitId);
  const containerRef = useRef(null);
  const instanceRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) {
      return undefined;
    }
    let isMounted = true;
    loadBundle()
      .then((module) => {
        if (!isMounted || !containerRef.current) {
          return;
        }
        instanceRef.current = module.init(
          {
            messageOrigin: getMessageOrigin(),
            submitUrl: SUBMIT_URL,
            getSubmitClient: getAuthenticatedHttpClient,
            getEnrichment,
            variant: 'slot',
          },
          { container: containerRef.current },
        );
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
  }, [getEnrichment]);

  return <div ref={containerRef} className="feedback-drawer-slot-wrapper" />;
};

export default FeedbackDrawerSlot;
