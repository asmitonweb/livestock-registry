import { widgetRegistry } from '@openg2p/registry-widgets';
import { GeoPointWidget } from './GeoPointWidget';

// Native defaults remain registered; add only the domain map widget.
widgetRegistry.register({ widget: 'oan-geo-point', component: GeoPointWidget as never });

import { IdentityVerificationWidget } from './IdentityVerificationWidget';
widgetRegistry.register({ widget: 'oan-identity-verification', component: IdentityVerificationWidget as never });
