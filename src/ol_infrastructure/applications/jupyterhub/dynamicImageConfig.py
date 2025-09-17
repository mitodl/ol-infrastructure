import os

from kubespawner import KubeSpawner

GPU_NODE_AFFINITY_LIST = [
    {
        "matchExpressions": [
            {
                "key": "ol.mit.edu/gpu_node",
                "operator": "In",
                "values": ["true"],
            }
        ]
    }
]

KNOWN_COURSES = [
    "clustering_and_descriptive_ai",
    "deep_learning_foundations_and_applications",
    "supervised_learning_fundamentals",
    "introduction_to_data_analytics_and_machine_learning",
]
# We have notebooks in UAI courses 6-13, excluding 10
KNOWN_COURSES.extend([f"uai_source-uai.{i}" for i in [6, 7, 8, 9, 11, 12, 13]])
# All courses which use the CUDA pytorch base image are below
GPU_ENABLED_COURSES = {
    "deep_learning_foundations_and_applications",
    "uai_source-uai.8",
    "uai_source-uai.9",
    "uai_source-uai.11",
    "uai_source-uai.12",
    "uai_source-uai.13",
}


class QueryStringKubeSpawner(KubeSpawner):
    def start(self):
        image_base = (
            "610119931565.dkr.ecr.us-east-1.amazonaws.com/ol-course-notebooks:{}"
        )
        self.image = (
            "610119931565.dkr.ecr.us-east-1.amazonaws.com/"
            "ol-course-notebooks:clustering_and_descriptive_ai"
        )
        if self.handler:
            course = self.handler.get_query_argument("course", "").lower()
            # Notebook is case sensitive, and special characters
            # with query string meaning must be URL encoded i.e.
            # notebook=Assignment_2_Prediction_with_LogReg_%26_CART_%26_XGBoost.ipynb
            notebook = self.handler.get_query_argument("notebook", "")
            tag = self.handler.get_query_argument("tag", "").lower()
            if course in KNOWN_COURSES or tag:
                # Specifying tag will let you effectively
                # reference anything in the ECR registry
                # Is this an issue? If so, we should gate it to QA/CI envs or remove it.
                self.image = (
                    image_base.format(tag) if tag else image_base.format(course)
                )

                if course in GPU_ENABLED_COURSES:
                    # This course requires a GPU, so we are adding a node affinity
                    # rule to schedule the pod on a node with a GPU.
                    self.node_affinity_required = GPU_NODE_AFFINITY_LIST

                # If we don't have a notebook, don't muck with default_url
                # This falls back to the tree view in Jupyterhub if not specified
                if notebook and notebook.endswith(".ipynb"):
                    self.default_url = f"/notebooks/{notebook}"
        return super().start()


c.JupyterHub.spawner_class = QueryStringKubeSpawner  # type: ignore[name-defined] # noqa: F821
c.Authenticator.allow_all = True  # type: ignore[name-defined] # noqa: F821
c.JupyterHub.db_url = os.environ["DATABASE_URL"]  # type: ignore[name-defined] # noqa: F821
