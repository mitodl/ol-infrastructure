import { useEffect, useRef, useState } from 'react';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { initAiDrawerManager } from '@mitodl/smoot-design/ai';

/**
 * Renders the AiDrawerManager in a sidebar slot using the "slot" variant.
 * The component uses the init function to avoid React version conflicts
 * between frontend-app-learning (React 18) and smoot-design (React 19).
 */
const AiDrawerManagerSidebar = () => {
  const containerRef = useRef(null);
  const instanceRef = useRef(null);
  const [initError, setInitError] = useState(null);

  useEffect(() => {
    // Wait for container ref to be available
    if (!containerRef.current) {
      return;
    }

    // Prevent duplicate initialization
    if (instanceRef.current) {
      return;
    }
    const lmsBaseUrl = getConfig().LMS_BASE_URL;
    let messageOrigin = window.location.origin;

    if (lmsBaseUrl) {
      try {
        messageOrigin = new URL(lmsBaseUrl).origin;
      } catch (error) {
        // Fallback to window.location.origin if LMS_BASE_URL is invalid
        if (process.env.NODE_ENV === 'development') {
          // eslint-disable-next-line no-console
          console.warn(
            'Invalid LMS_BASE_URL, using window.location.origin for messageOrigin',
            error,
          );
        }
      }
    }

    let cleanup;

    try {
      const initResult = initAiDrawerManager(
        {
          messageOrigin,
          variant: 'slot', // Render in slot instead of overlay drawer
          getTrackingClient: () => getAuthenticatedHttpClient(),
        },
        {
          container: containerRef.current,
        },
      );

      instanceRef.current = initResult;
      setInitError(null);

      cleanup = () => {
        if (instanceRef.current?.unmount) {
          try {
            instanceRef.current.unmount();
          } catch (error) {
            // Unmount function handles all errors internally
            if (process.env.NODE_ENV === 'development') {
              // eslint-disable-next-line no-console
              console.warn('Error during AiDrawerManager unmount (safely handled):', error);
            }
          }
        }
        instanceRef.current = null;
      };
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Failed to initialize AiDrawerManager';
      setInitError(errorMessage);

      if (process.env.NODE_ENV === 'development') {
        // eslint-disable-next-line no-console
        console.error('Failed to initialize AiDrawerManager:', error);
      }
    }

    return cleanup;
  }, []);

  return (
    <div
      ref={containerRef}
      className="ai-drawer-manager-sidebar-wrapper"
      style={{ minHeight: 0 }}
      role="region"
      aria-label="AI Chat Sidebar"
    >
      {initError && (
        <div className="alert alert-danger" role="alert">
          <strong>Error loading AI Chat:</strong> {initError}
        </div>
      )}
    </div>
  );
};

export default AiDrawerManagerSidebar;
