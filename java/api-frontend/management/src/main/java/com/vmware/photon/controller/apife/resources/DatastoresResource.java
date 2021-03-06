/*
 * Copyright 2015 VMware, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may not
 * use this file except in compliance with the License.  You may obtain a copy of
 * the License at http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software distributed
 * under the License is distributed on an "AS IS" BASIS, without warranties or
 * conditions of any kind, EITHER EXPRESS OR IMPLIED.  See the License for the
 * specific language governing permissions and limitations under the License.
 */

package com.vmware.photon.controller.apife.resources;

import com.vmware.photon.controller.api.Datastore;
import com.vmware.photon.controller.api.ResourceList;
import com.vmware.photon.controller.api.common.exceptions.external.ExternalException;
import com.vmware.photon.controller.apife.clients.DatastoreFeClient;
import com.vmware.photon.controller.apife.config.PaginationConfig;
import com.vmware.photon.controller.apife.resources.routes.DatastoreResourceRoutes;
import com.vmware.photon.controller.apife.utils.PaginationUtils;
import static com.vmware.photon.controller.api.common.Responses.generateResourceListResponse;

import com.google.common.base.Optional;
import com.google.inject.Inject;
import com.wordnik.swagger.annotations.Api;
import com.wordnik.swagger.annotations.ApiOperation;
import com.wordnik.swagger.annotations.ApiResponse;
import com.wordnik.swagger.annotations.ApiResponses;
import org.glassfish.jersey.server.ContainerRequest;

import javax.ws.rs.Consumes;
import javax.ws.rs.GET;
import javax.ws.rs.Path;
import javax.ws.rs.Produces;
import javax.ws.rs.QueryParam;
import javax.ws.rs.core.Context;
import javax.ws.rs.core.MediaType;
import javax.ws.rs.core.Request;
import javax.ws.rs.core.Response;

/**
 * Inventory datastore resource api def.
 */
@Path(DatastoreResourceRoutes.API)
@Api(value = DatastoreResourceRoutes.API)
@Produces(MediaType.APPLICATION_JSON)
@Consumes(MediaType.APPLICATION_JSON)
public class DatastoresResource {

  private final DatastoreFeClient datastoreFeClient;
  private final PaginationConfig paginationConfig;

  @Inject
  public DatastoresResource(DatastoreFeClient datastoreFeClient, PaginationConfig paginationConfig) {

    this.datastoreFeClient = datastoreFeClient;
    this.paginationConfig = paginationConfig;
  }

  @GET
  @ApiOperation(value = "Enumerate all datastores", response = Datastore.class,
      responseContainer = ResourceList.CLASS_NAME)
  @ApiResponses(value = {@ApiResponse(code = 200, message = "Success")})
  public Response find(@Context Request request,
                       @QueryParam("tag") Optional<String> tag,
                       @QueryParam("pageSize") Optional<Integer> pageSize,
                       @QueryParam("pageLink") Optional<String> pageLink)  throws ExternalException {
    ResourceList<Datastore> resourceList;
    if (pageLink.isPresent()) {
      resourceList = datastoreFeClient.getDatastoresPage(pageLink.get());
    } else {
      Optional<Integer> adjustedPageSize = PaginationUtils.determinePageSize(paginationConfig, pageSize);
      resourceList = datastoreFeClient.find(tag, adjustedPageSize);
    }

    return generateResourceListResponse(
        Response.Status.OK,
        PaginationUtils.formalizePageLinks(resourceList, DatastoreResourceRoutes.API),
        (ContainerRequest) request,
        DatastoreResourceRoutes.DATASTORE_PATH);
  }

}
