package bmp

import (
	bmpProvisioning "bmp/provisioning"
	"bytes"
	"dcp/client"
	"dcp/common"
	"dcp/operation"
	dcpProvisioning "dcp/provisioning"
	"dcp/uri"
	"encoding/json"
	"path"
	"slingshot/types"

	"golang.org/x/net/context"
	"dcp/provisioning"
	"github.com/pborman/uuid"
)

const (
	CloudStorePath  = "/photon/cloudstore"
	SubnetPath        = CloudStorePath + "/dhcp-subnets"
	LeasePath         = CloudStorePath + "/dhcp-leases"
	ConfigurationPath = CloudStorePath + "/dhcp-host-configuration"
)

type Client struct {
	URI uri.URI
}

func NewClient() *Client {
	//return &Client{uri.Local()}
	return &Client{uri.New("172.31.253.66", "19000")}
}

// CreateSubnet creates a subnet.
func (b *Client) CreateSubnet(in types.Subnet) (*types.Subnet, error) {
	out := &types.Subnet{}

	if in.SelfLink == "" {
		in.SelfLink = in.Subnet.String()
	}

	u := uri.Extend(b.URI, SubnetPath)
	t, _ := json.Marshal(in)
	buf := bytes.NewBuffer(t)

	ctx := context.TODO()
	op := operation.NewPost(ctx, u, buf)
	if err := client.Send(op).Wait(); err != nil {
		return nil, err
	}

	if err := op.DecodeBody(out); err != nil {
		return nil, err
	}

	return out, nil
}

// ListSubnets returns a list of subnets.
func (b *Client) ListSubnets() ([]types.Subnet, error) {
	var subnets []types.Subnet

	u := uri.Extend(b.URI, SubnetPath)
	u = uri.ExtendQuery(u, dcpProvisioning.QueryExpand, dcpProvisioning.QueryDocumentSelfLinkLink)

	ctx := context.TODO()
	op := operation.NewGet(ctx, u)
	if err := client.Send(op).Wait(); err != nil {
		return nil, err
	}

	type subnetQueryResult struct {
		common.ServiceDocumentQueryResult
		Documents map[string]types.Subnet `json:"documents,omitempty"`
	}
	var s subnetQueryResult
	if err := op.DecodeBody(&s); err != nil {
		return nil, err
	}

	for _, v := range s.Documents {
		subnets = append(subnets, v)
	}
	return subnets, nil
}

// AcquireLease acquires a lease. The returned lease can be a static lease that
// existed prior to this call, or it can be created dynamically by the subnet
// service.
func (b *Client) AcquireLease(subnetSelfLink string, mac types.MAC) (*types.Lease, error) {
	ctx := context.TODO()
	u := uri.Extend(b.URI, subnetSelfLink)
	return bmpProvisioning.AcquireLease(ctx, u, mac)
}

// LoadConfiguration returns a configuration by its id
func (b *Client) LoadConfiguration(id string, configuration *types.Configuration) error {
	ctx := context.TODO()
	u := uri.Extend(b.URI, path.Join(ConfigurationPath, id))
	op := operation.NewGet(ctx, u)
	if err := client.Send(op).Wait(); err != nil {
		return err
	}

	return op.DecodeBody(&configuration)
}

func (b *Client) LoadStatesFromConfiguration(c *types.Configuration) (*dcpProvisioning.ComputeState, *dcpProvisioning.DiskState, error) {
//	computeReference, ok := c.Data["computeStateReference"]
//	if !ok {
//		return nil, nil, fmt.Errorf("missing attribute: computeStateReference")
//	}
//
//	computeReferenceURI, err := uri.Parse(computeReference)
//	if err != nil {
//		return nil, nil, err
//	}
//
//	diskReference, ok := c.Data["diskStateReference"]
//	if !ok {
//		return nil, nil, fmt.Errorf("missing attribute: diskStateReference")
//	}
//
//	diskReferenceURI, err := uri.Parse(diskReference)
//	if err != nil {
//		return nil, nil, err
//	}
//
//	// Send GETs in parallel
//	ctx := context.TODO()
//	computeOp := client.Send(operation.NewGet(ctx, computeReferenceURI))
//	diskOp := client.Send(operation.NewGet(ctx, diskReferenceURI))
//
//	// Both states are needed so we can wait for them sequentially
//	if err := computeOp.Wait(); err != nil {
//		return nil, nil, err
//	}
//
//	if err := diskOp.Wait(); err != nil {
//		return nil, nil, err
//	}

//	var compute dcpProvisioning.ComputeState
//	if err := computeOp.DecodeBody(&compute); err != nil {
//		return nil, nil, err
//	}
//
//	var disk dcpProvisioning.DiskState
//	if err := diskOp.DecodeBody(&disk); err != nil {
//		return nil, nil, err
//	}

	compute := dcpProvisioning.ComputeState{
			ID: uuid.New(),
		}

	disk := dcpProvisioning.DiskState{
			ID:       uuid.New(),
			DiskType: provisioning.BootDeviceNetwork,
		   SourceImageReference: "http://artifactory.ec.eng.vmware.com/artifactory/esxcloud-archives/userContent/tmp/serverimage.tar",
			/*BootConfig: test.CoreosCloudConfig,

			CustomizationServiceReference: uri.Extend(uri.Local(), provisioning.BootConfigServiceIso).String(),*/
		}

	return &compute, &disk, nil
}
