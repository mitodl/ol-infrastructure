import { useContext, useEffect, useRef } from 'react';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import SidebarContext from './src/courseware/course/sidebar/SidebarContext';
import { loadBundle, getMessageOrigin, SUBMIT_URL } from './feedbackBundle';
import useFeedbackEnrichment from './useFeedbackEnrichment';

const FeedbackDrawerManagerSidebar = () => {
  const { courseId = null, unitId = null } = useContext(SidebarContext) ?? {};
  const getEnrichment = useFeedbackEnrichment(courseId, unitId);
  const instanceRef = useRef(null);

  useEffect(() => {
    let isMounted = true;
    loadBundle()
      .then((module) => {
        if (!isMounted) { return; }
        instanceRef.current = module.init({
          messageOrigin: getMessageOrigin(),
          submitUrl: SUBMIT_URL,
          getSubmitClient: getAuthenticatedHttpClient,
          getEnrichment,
          variant: 'drawer',
        });
      })
      .catch(() => { /* bundle load/init failure is non-fatal for the page */ });
    return () => {
      isMounted = false;
      if (instanceRef.current?.unmount) {
        try { instanceRef.current.unmount(); } catch (error) { /* unmount handles its own errors */ }
      }
      instanceRef.current = null;
    };
  }, [getEnrichment]);

  return null;
};

export default FeedbackDrawerManagerSidebar;
