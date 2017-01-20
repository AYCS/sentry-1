from __future__ import absolute_import

from django.db import IntegrityError, transaction

from rest_framework.response import Response

from .project_releases import ReleaseSerializer
from sentry.api.base import DocSection
from sentry.api.bases.organization import OrganizationEndpoint, OrganizationReleasePermission
from sentry.api.paginator import OffsetPaginator
from sentry.api.serializers import serialize
from sentry.api.serializers.rest_framework import ListField
from sentry.app import locks
from sentry.models import Activity, OrganizationMemberTeam, Project, Release, ReleaseProject, Team
from sentry.utils.retries import TimedRetryPolicy


class ReleaseSerializerWithProjects(ReleaseSerializer):
    projects = ListField()


class OrganizationReleasesEndpoint(OrganizationEndpoint):
    doc_section = DocSection.RELEASES
    permission_classes = (OrganizationReleasePermission,)

    def get_allowed_projects(self, request, organization):
        if not request.user.is_authenticated():
            return []

        if request.is_superuser() or organization.flags.allow_joinleave:
            allowed_teams = Team.objects.filter(
                organization=organization
            ).values_list('id', flat=True)
        else:
            allowed_teams = OrganizationMemberTeam.objects.filter(
                organizationmember__user=request.user,
                team__organization_id=organization.id,
            ).values_list('team_id', flat=True)
        return Project.objects.filter(team_id__in=allowed_teams)

    def get(self, request, organization):
        """
        List an Organizations Releases
        ``````````````````````````````
        Return a list of releases for a given organization.

        :pparam string organization_slug: the organization short name
        :qparam string query: this parameter can be used to create a
                              "starts with" filter for the version.
        """
        query = request.GET.get('query')

        allowed_projects = self.get_allowed_projects(request, organization)

        queryset = Release.objects.filter(
            organization=organization,
            projects=allowed_projects
        ).select_related('owner')

        if query:
            queryset = queryset.filter(
                version__istartswith=query,
            )

        queryset = queryset.extra(select={
            'sort': 'COALESCE(date_released, date_added)',
        })

        return self.paginate(
            request=request,
            queryset=queryset,
            order_by='-sort',
            paginator_cls=OffsetPaginator,
            on_results=lambda x: serialize(x, request.user),
        )

    def post(self, request, organization):
        """
        Create a New Release for an Organization
        ````````````````````````````````````````
        Create a new release for the given Organization.  Releases are used by
        Sentry to improve its error reporting abilities by correlating
        first seen events with the release that might have introduced the
        problem.
        Releases are also necessary for sourcemaps and other debug features
        that require manual upload for functioning well.
        :pparam string organization_slug: the slug of the organization the
                                          release belongs to.
        :param string version: a version identifier for this release.  Can
                               be a version number, a commit hash etc.
        :param string ref: an optional commit reference.  This is useful if
                           a tagged version has been provided.
        :param url url: a URL that points to the release.  This can be the
                        path to an online interface to the sourcecode
                        for instance.
        :param array projects: a list of project slugs that are involved in
                               this release
        :param datetime dateStarted: an optional date that indicates when the
                                     release process started.
        :param datetime dateReleased: an optional date that indicates when
                                      the release went live.  If not provided
                                      the current time is assumed.
        :auth: required
        """
        serializer = ReleaseSerializerWithProjects(data=request.DATA)

        if serializer.is_valid():
            result = serializer.object

            projects = Project.objects.filter(organization=organization,
                                              slug__in=result['projects'])
            invalid_projects = set(result['projects']) - {p.slug for p in projects}
            if invalid_projects:
                return Response({'projects': ['Invalid project slugs']}, status=400)

            # release creation is idempotent to simplify user
            # experiences
            release = Release.objects.filter(
                organization_id=organization.id,
                version=result['version']
            ).first()
            if release:
                created = False
            else:
                lock_key = Release.get_lock_key(organization.id, result['version'])
                lock = locks.get(lock_key, duration=5)
                with TimedRetryPolicy(10)(lock.acquire):
                    try:
                        release, created = Release.objects.get(
                            version=result['version'],
                            organization_id=organization.id
                        ), False
                    except Release.DoesNotExist:
                        release, created = Release.objects.create(
                            organization_id=organization.id,
                            version=result['version'],
                            ref=result.get('ref'),
                            url=result.get('url'),
                            owner=result.get('owner'),
                            date_started=result.get('dateStarted'),
                            date_released=result.get('dateReleased'),
                        ), True

            new_projects = []
            for project in projects:
                try:
                    with transaction.atomic():
                        ReleaseProject.objects.create(project=project, release=release)
                except IntegrityError:
                    pass
                else:
                    new_projects.append(project)

            if release.date_released:
                for project in new_projects:
                    activity = Activity.objects.create(
                        type=Activity.RELEASE,
                        project=project,
                        ident=result['version'],
                        data={'version': result['version']},
                        datetime=release.date_released,
                    )
                    activity.send_notification()

            commit_list = result.get('commits')
            if commit_list:
                release.set_commits(commit_list)

            if not created:
                # This is the closest status code that makes sense, and we want
                # a unique 2xx response code so people can understand when
                # behavior differs.
                #   208 Already Reported (WebDAV; RFC 5842)
                status = 208
            else:
                status = 201

            return Response(serialize(release, request.user), status=status)
        return Response(serializer.errors, status=400)
