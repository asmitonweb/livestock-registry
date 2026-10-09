
pipeline {
    agent { label 'vpn-agent2' }

    options {
        // One run at a time: merging several PRs back to back queued
        // overlapping runs whose helm upgrades collided ("another
        // operation in progress"), which is what showed up as a failed
        // db-seed on dev on 2026-09-18.
        disableConcurrentBuilds()
    }

    environment {
        AWS_REGION       = 'ap-south-1'
        ECR_PATH         = 'openg2p/livestock-registry'
        RP_VERSION       = '1.2.2-rc.544' // TODO verify — see header note
        HELM_RELEASE     = 'livestock-registry'
        HELM_NAMESPACE   = 'live'
        // The chart actually running in staging's `live` today — NOT this
        // repo's own helm/openg2p-livestock-registry. See header comment.
        HELM_CHART_REPO  = 'openg2p'
        HELM_CHART_URL   = 'https://openg2p.github.io/openg2p-helm'
        HELM_CHART_REF   = 'openg2p/openg2p-farmer-registry'
        HELM_CHART_VER   = '1.2.0'

        // Dev-cluster deploy target (see header comment above). Separate
        // physical cluster from HELM_NAMESPACE/staging-rke2-kubeconfig
        // above; same chart/release-name convention, different kubeconfig.
        // Dedicated credential for the dedicated live:livestock-ci
        // ServiceAccount (ci/k8s/livestock-deploy-rbac.yaml) — NOT
        // gen2-dev-kubeconfig, which is farmer-ci's own token scoped to
        // `far` only and is untouched by this pipeline.
        HELM_NAMESPACE_DEV       = 'live'
        DEV_KUBECONFIG_CRED_ID   = 'gen2-dev-livestock-kubeconfig'

        // livestock-registry-dashboard-api lives in its own (public) repository
        // and is built here beside the registry images. Each branch builds the
        // dashboard-api branch of the same name and falls back to develop where
        // there is none. Set DASHBOARD_API_REF on the job (a branch or tag) to
        // pin one instead.
        DASHBOARD_API_REPO = 'https://github.com/Centre-for-Open-Societal-Systems/livestock-registry-dashbaord-api.git'
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Checkout dashboard-api') {
            // TEMPORARY: livestock-registry-dashboard-api has not been merged to its
            // staging branch yet, so staging builds skip it (Deploy to Live never
            // uses the dashboard-api image). Drop this `when` once it is merged.
            when { not { branch 'staging' } }
            steps {
                script {
                    // A PR build (BRANCH_NAME PR-<n>) matches on its source branch.
                    def ref = env.DASHBOARD_API_REF
                    if (!ref) {
                        def wanted = env.CHANGE_BRANCH ?: env.BRANCH_NAME
                        def found = wanted && sh(returnStatus: true,
                            script: "git ls-remote --exit-code --heads ${DASHBOARD_API_REPO} 'refs/heads/${wanted}' > /dev/null") == 0
                        ref = found ? wanted : 'develop'
                    }
                    // .build/ is git-ignored, so the clone never enters a commit.
                    sh "rm -rf .build/dashboard-api && git clone --quiet --depth 1 --branch '${ref}' ${DASHBOARD_API_REPO} .build/dashboard-api"
                    if (!fileExists('.build/dashboard-api/Dockerfile')) {
                        error("dashboard-api ${ref} has no Dockerfile: merge the service into that branch of ${DASHBOARD_API_REPO}, or set DASHBOARD_API_REF")
                    }
                    env.DASHBOARD_API_REF_USED = ref
                    env.DASHBOARD_API_SHA = sh(returnStdout: true, script: 'git -C .build/dashboard-api rev-parse --short=12 HEAD').trim()
                    echo "dashboard-api: ${ref} @ ${env.DASHBOARD_API_SHA}"
                }
            }
        }

        stage('Build & Push Images') {
            steps {
                withCredentials([
                    string(credentialsId: 'AWS_ACCOUNT_ID', variable: 'AWS_ACCOUNT_ID'),
                    [$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']
                ]) {
                    script {
                        env.ECR_REGISTRY = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
                        env.IMAGE_TAG    = env.GIT_COMMIT.take(12)
                    }
                    sh '''
                        echo "=== Logging in to ECR ==="
                        aws ecr get-login-password --region ${AWS_REGION} | \
                            docker login --username AWS --password-stdin ${ECR_REGISTRY}
                    '''
                    script {
                        def components = ['staff-api', 'partner-api', 'celery', 'db-seed', 'sanity-tests', 'staff-ui']
                        components.each { name ->
                            def image = "${env.ECR_REGISTRY}/${ECR_PATH}/${name}:${env.IMAGE_TAG}"
                            sh """
                                echo "=== Building and pushing ${name} ==="
                                docker build --build-arg RP_VERSION=${RP_VERSION} \
                                    -f docker/${name}/Dockerfile -t ${image} --no-cache .
                                docker push ${image}
                            """
                        }

                        // Skipped when 'Checkout dashboard-api' did not run (staging).
                        if (env.DASHBOARD_API_SHA) {
                            // The dashboard service, from its own repository (cloned
                            // by 'Checkout dashboard-api'), with its own build context.
                            // Its ECR repository is created outside CI, like the others.
                            def api = "${env.ECR_REGISTRY}/${ECR_PATH}/dashboard-api:${env.IMAGE_TAG}"
                            sh """
                                echo "=== Building and pushing dashboard-api (${env.DASHBOARD_API_REF_USED} @ ${env.DASHBOARD_API_SHA}) ==="
                                docker build \
                                    --label org.opencontainers.image.source=${DASHBOARD_API_REPO} \
                                    --label org.opencontainers.image.revision=${env.DASHBOARD_API_SHA} \
                                    --label org.opencontainers.image.ref.name=${env.DASHBOARD_API_REF_USED} \
                                    -f .build/dashboard-api/Dockerfile -t ${api} .build/dashboard-api
                                docker push ${api}
                            """
                        }
                    }
                }
            }
        }

        stage('Prepare Deploy Values') {
            // Split out 2026-09-16 so both deploy targets (dev cluster and
            // staging cluster's `live` namespace) share the exact same
            // image-tag overlay file instead of two copies that could drift.
            // Content is cluster/namespace-agnostic (image repo+tag only) —
            // safe to reuse verbatim against either kubeconfig.
            //
            // Gated on the SAME branch as "Deploy to Live" below (not
            // 'develop' anymore) — this stage exists solely to feed that
            // one. See the branch-gating header note above.
            when { branch 'staging' } // ASSUMPTION — confirm the real staging branch name
            steps {
                sh """
                    cat > /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml <<EOF
registry:
  staffApi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/staff-api
      tag: "${env.IMAGE_TAG}"
  partnerApi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/partner-api
      tag: "${env.IMAGE_TAG}"
  celeryWorker:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  celeryBeat:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  dbSeed:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/db-seed
      tag: "${env.IMAGE_TAG}"
  staffUi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/staff-ui
      tag: "${env.IMAGE_TAG}"
  sanity:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/sanity-tests
      tag: "${env.IMAGE_TAG}"
EOF
                """
            }
        }

        stage('Deploy to Development') {
            // Unchanged by the branch-gating edit — still runs on every
            // `develop` push, which is correct per the user (this is the
            // stage that should keep working exactly as it does today).
            //
            // Uses THIS repo's own chart (helm/openg2p-livestock-registry)
            // with its checked-in values.yaml PLUS a dev-specific overlay,
            // values-dev.yaml (hostname/MinIO/id-generator GitLab path/
            // celery memory — see header UPDATE note for the full
            // rationale and the one-time manual VirtualService cleanup
            // documented in README-dev.md). Only the six image refs are
            // still overridden via --set, matching the Build & Push stage.
            //
            // `helm upgrade --install` (not `--reuse-values`) is used
            // deliberately: every run supplies the full intended value set
            // (values.yaml + values-dev.yaml + these --set flags) from repo
            // state, so there's nothing to drift out of sync with and no
            // dependency on a prior release already existing.
            //
            // The `grep -q openg2p\\.org` check below is a regression
            // guard: if values-dev.yaml's hostname override somehow didn't
            // take effect, the release would silently go back to being
            // unreachable (see header UPDATE note) — this fails the build
            // loudly, before any real cluster mutation, instead of
            // deploying something broken again.
            //
            // Wrapped in catchError so a problem specific to the dev
            // cluster does NOT block the staging "Deploy to Live" stage
            // below from running.
            when { branch 'develop' }
            steps {
                catchError(buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
                    withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG')]) {
                        sh """
                            helm repo add ${HELM_CHART_REPO} ${HELM_CHART_URL} || true
                            helm repo update ${HELM_CHART_REPO}
                            helm dependency build ./helm/openg2p-livestock-registry

                            IMAGE_SET_FLAGS="
                                --set registry.staffApi.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/staff-api
                                --set registry.staffApi.image.tag=${env.IMAGE_TAG}
                                --set registry.partnerApi.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/partner-api
                                --set registry.partnerApi.image.tag=${env.IMAGE_TAG}
                                --set registry.celeryWorker.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/celery
                                --set registry.celeryWorker.image.tag=${env.IMAGE_TAG}
                                --set registry.celeryBeat.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/celery
                                --set registry.celeryBeat.image.tag=${env.IMAGE_TAG}
                                --set registry.dbSeed.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/db-seed
                                --set registry.dbSeed.image.tag=${env.IMAGE_TAG}
                                --set registry.staffUi.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/staff-ui
                                --set registry.staffUi.image.tag=${env.IMAGE_TAG}
                                --set registry.sanity.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/sanity-tests
                                --set registry.sanity.image.tag=${env.IMAGE_TAG}
                                --set reporting.views.enabled=true
                                --set dashboardApi.enabled=true
                                --set dashboardApi.image.repository=${env.ECR_REGISTRY}/${ECR_PATH}/dashboard-api
                                --set dashboardApi.image.tag=${env.IMAGE_TAG}
                                --set dashboardApi.virtualService.enabled=true
                                --set dashboardApi.virtualService.host=dashboard-api.${HELM_NAMESPACE_DEV}.openg2p.test
                                --set dashboardApi.virtualService.gateway=internal
                            "

                            helm template \${HELM_RELEASE} ./helm/openg2p-livestock-registry -n \${HELM_NAMESPACE_DEV} \
                                -f ./helm/openg2p-livestock-registry/values-dev.yaml \
                                \$IMAGE_SET_FLAGS \
                                > dev-rendered-\${BUILD_NUMBER}.yaml
                            echo "Rendered \$(wc -l < dev-rendered-\${BUILD_NUMBER}.yaml) lines from the in-repo chart's values.yaml + values-dev.yaml + image overrides. Archived for audit."

                            if grep -q 'openg2p\\.org' dev-rendered-\${BUILD_NUMBER}.yaml; then
                                echo "FATAL: rendered manifests still reference the placeholder domain openg2p.org:"
                                grep -n 'openg2p\\.org' dev-rendered-\${BUILD_NUMBER}.yaml | head -20
                                exit 1
                            fi

                            helm upgrade --install \${HELM_RELEASE} ./helm/openg2p-livestock-registry -n \${HELM_NAMESPACE_DEV} \
                                -f ./helm/openg2p-livestock-registry/values-dev.yaml \
                                \$IMAGE_SET_FLAGS \
                                --cleanup-on-fail --timeout 10m

                            kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-api -n \${HELM_NAMESPACE_DEV} --timeout=180s
                            kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-ui -n \${HELM_NAMESPACE_DEV} --timeout=180s
                            kubectl rollout status deployment/\${HELM_RELEASE}-partner-api -n \${HELM_NAMESPACE_DEV} --timeout=180s
                            kubectl rollout status deployment/\${HELM_RELEASE}-dashboard-api -n \${HELM_NAMESPACE_DEV} --timeout=180s

                            # The views exist (the reporting hook ran) and the service
                            # answers: a 500 from a missing lr_rpt_* view fails here,
                            # not in the dashboards.
                            echo "=== dashboard-api smoke test ==="
                            kubectl exec -n \${HELM_NAMESPACE_DEV} deploy/\${HELM_RELEASE}-dashboard-api -- python -c "import json, urllib.request as u; base = 'http://127.0.0.1:8000'; [print(p, 'OK', len(json.load(u.urlopen(base + p, timeout=30)))) for p in ('/health', '/api/v1/charts/livestockKpis', '/api/v1/charts/livestockBySpecies', '/api/v1/charts/livestockKeepersByRegion')]"
                        """
                        archiveArtifacts artifacts: 'dev-rendered-*.yaml', allowEmptyArchive: true
                    }
                }
            }
        }

        stage('Deploy to Live') {
            // CHANGED 2026-09-22: was `when { branch 'develop' }`. The user
            // confirmed develop pushes should only touch the dev cluster —
            // this stage (staging) now only runs on pushes to
            // '<STAGING_BRANCH>' (placeholder: 'main', NOT yet confirmed by
            // name — see the header note at the top of this file). Note
            // this doesn't fix the build #41 db-seed BackoffLimitExceeded
            // failure; it just stops it from being triggered by develop.
            when { branch 'staging' } // ASSUMPTION — confirm the real staging branch name
            steps {
                withCredentials([file(credentialsId: 'staging-rke2-kubeconfig', variable: 'KUBECONFIG')]) {
                    sh """
                        helm repo add ${HELM_CHART_REPO} ${HELM_CHART_URL} || true
                        helm repo update ${HELM_CHART_REPO}

                        # Dry-run + diff, kept even without a human gate so there's an
                        # audit trail to look at if a deploy ever needs investigating.
                        #
                        # NOTE: `helm template` has NO --reuse-values flag -- that flag
                        # only exists on `helm upgrade`/`helm install`, which manage a
                        # stored release to reuse values from. Fixed by explicitly
                        # feeding the just-captured live values back in as a -f file
                        # instead -- this is the actual on-disk equivalent of what
                        # --reuse-values does internally on a real upgrade.
                        helm get values \${HELM_RELEASE} -n \${HELM_NAMESPACE} -a -o yaml > live-values-before-\${BUILD_NUMBER}.yaml
                        helm template \${HELM_RELEASE} ${HELM_CHART_REF} --version ${HELM_CHART_VER} -n \${HELM_NAMESPACE} \
                            -f live-values-before-\${BUILD_NUMBER}.yaml \
                            -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml \
                            > live-rendered-\${BUILD_NUMBER}.yaml
                        echo "Rendered \$(wc -l < live-rendered-\${BUILD_NUMBER}.yaml) lines against the currently-deployed values (image tags only overridden). Archived for audit."

                        # No --atomic/--wait: `live`'s pre-existing, not-fixable-from-here
                        # id-generator GitLab-403 pull failure means its ReplicaSet can
                        # never finish rolling out, so a --wait'd upgrade would block for
                        # the full --timeout and --atomic would auto-roll-back every run.
                        # --cleanup-on-fail is kept for some safety net on a genuine hook
                        # failure; the explicit `kubectl rollout status` checks below are
                        # the real verification step, scoped to the three deployments
                        # this pipeline actually cares about.
                        helm upgrade \${HELM_RELEASE} ${HELM_CHART_REF} --version ${HELM_CHART_VER} -n \${HELM_NAMESPACE} \
                            --reuse-values -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml \
                            --cleanup-on-fail --timeout 10m

                        kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-api -n \${HELM_NAMESPACE} --timeout=180s
                        kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-ui -n \${HELM_NAMESPACE} --timeout=180s
                        kubectl rollout status deployment/\${HELM_RELEASE}-partner-api -n \${HELM_NAMESPACE} --timeout=180s
                    """
                    archiveArtifacts artifacts: 'live-values-before-*.yaml, live-rendered-*.yaml', allowEmptyArchive: true
                }
            }
        }
    }

    post {
        success {
            script {
                def committerEmail = sh(script: "git log -1 --pretty=format:'%ae'", returnStdout: true).trim()
                def committerName  = sh(script: "git log -1 --pretty=format:'%an'", returnStdout: true).trim()
                if (committerEmail.contains('noreply')) {
                    committerEmail = 'devops@yourorg.com'
                }
                mail(
                    to: committerEmail,
                    subject: "✅ Build SUCCESS: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                    body: """
Hi ${committerName},

Livestock Registry build/deploy succeeded!

Job:    ${env.JOB_NAME}
Branch: ${env.GIT_BRANCH}
Build:  #${env.BUILD_NUMBER}
URL:    ${env.BUILD_URL}

Regards,
Jenkins
"""
                )
            }
        }
        failure {
            script {
                def committerEmail = sh(script: "git log -1 --pretty=format:'%ae'", returnStdout: true).trim()
                def committerName  = sh(script: "git log -1 --pretty=format:'%an'", returnStdout: true).trim()
                if (committerEmail.contains('noreply')) {
                    committerEmail = 'simretyibeltal@gmail.com, Pavan.ns@gmail.com'
                }
                mail(
                    to: committerEmail,
                    subject: "❌ Build FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                    body: """
Hi ${committerName},

Livestock Registry build or deployment failed. Deploy no longer uses
--atomic (dropped 2026-09-16 -- it was blocking on the already-broken
id-generator Deployment and auto-rolling-back every run), so a Deploy
failure does NOT automatically revert `live`. Check
`helm history livestock-registry -n live` and `helm status livestock-registry -n live`
to see exactly what state the release is in before assuming anything.

Job:    ${env.JOB_NAME}
Branch: ${env.GIT_BRANCH}
Build:  #${env.BUILD_NUMBER}
URL:    ${env.BUILD_URL}

Regards,
Jenkins
"""
                )
            }
        }
        always {
            sh 'docker image prune -f || true'
            sh 'docker logout ${ECR_REGISTRY} || true'
        }
    }
}
