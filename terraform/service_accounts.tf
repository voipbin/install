# Service account IAM bindings that are not co-located with their resource files.
#
# sa-kamailio         — defined in kamailio.tf
# sa-rtpengine        — defined in rtpengine.tf
# sa-gke-nodes        — defined in gke.tf
# sa-cloudsql-proxy   — defined in cloudsql.tf
#
# This file exists as a central reference. All service accounts and their
# IAM bindings are created alongside the resources that use them.
